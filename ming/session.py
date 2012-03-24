from __future__ import absolute_import
import logging
from functools import update_wrapper

import pymongo
import pymongo.errors

from .base import Cursor, Object
from .utils import fixup_index
from . import exc

log = logging.getLogger(__name__)

def annotate_doc_failure(func):
    '''Decorator to wrap a session operation so that any pymongo errors raised
    will note the document that caused the failure
    '''
    def wrapper(self, doc, *args, **kwargs):
        try:
            return func(self, doc, *args, **kwargs)
        except pymongo.errors.OperationFailure, opf:
            opf.args = opf.args + (('doc:  ' + str(doc)),)
            raise
    return update_wrapper(wrapper, func)

class Session(object):
    _registry = {}
    _datastores = {}

    def __init__(self, bind=None):
        self.bind = bind

    @classmethod
    def by_name(cls, name):
        if name in cls._registry:
            result = cls._registry[name]
        else:
            result = cls._registry[name] = cls(cls._datastores.get(name))
        return result

    def _impl(self, cls):
        try:
            return self.db[cls.m.collection_name]
        except TypeError:
            raise exc.MongoGone, 'MongoDB is not connected'

    @property
    def db(self):
        return self.bind.db

    def get(self, cls, **kwargs):
        bson = self._impl(cls).find_one(kwargs)
        if bson is None: return None
        return cls.make(bson, allow_extra=True, strip_extra=True)

    def find(self, cls, *args, **kwargs):
        allow_extra=kwargs.pop('allow_extra', True)
        strip_extra=kwargs.pop('strip_extra', True)
        validate=kwargs.pop('validate', True)
        collection = self._impl(cls)
        if not validate:
            return (
                cls(o, skip_from_bson=True)
                for o in collection.find(as_class=Object, *args, **kwargs))
        cursor = collection.find(*args, **kwargs)
        return Cursor(cls, cursor,
                      allow_extra=allow_extra,
                      strip_extra=strip_extra)

    def remove(self, cls, *args, **kwargs):
        if 'safe' not in kwargs:
            kwargs['safe'] = True
        for kwarg in kwargs:
            if kwarg not in ('spec_or_id', 'safe'):
                raise ValueError("Unexpected kwarg %s.  Did you mean to pass a dict?  If only sent kwargs, pymongo's remove()"
                                 " would've emptied the whole collection.  Which we're pretty sure you don't want." % kwarg)
        self._impl(cls).remove(*args, **kwargs)

    def find_by(self, cls, **kwargs):
        return self.find(cls, kwargs)

    def count(self, cls):
        return self._impl(cls).count()

    def ensure_index(self, cls, fields, **kwargs):
        index_fields = fixup_index(fields)
        return self._impl(cls).ensure_index(index_fields, **kwargs), fields

    def ensure_indexes(self, cls):
        for idx in cls.m.indexes:
            self.ensure_index(cls, idx.index_spec, unique=idx.unique,
                    sparse=idx.sparse)

    def group(self, cls, *args, **kwargs):
        return self._impl(cls).group(*args, **kwargs)

    def update_partial(self, cls, spec, fields, upsert=False, **kw):
        return self._impl(cls).update(spec, fields, upsert, safe=True, **kw)

    def find_and_modify(self, cls, query=None, sort=None, new=False, **kw):
        if query is None: query = {}
        if sort is None: sort = {}
        options = dict(kw, query=query, sort=sort, new=new)
        bson = self._impl(cls).find_and_modify(**options)
        if bson is None: return None
        return cls.make(bson)

    def _prep_save(self, doc, validate):
        hook = doc.m.before_save
        if hook: hook(doc)
        if validate:
            doc.make_safe()
            if doc.m.schema is None:
                data = dict(doc)
            else:
                data = doc.m.schema.validate(doc)
            doc.update(data)
        else:
            data =dict(doc)
        return data

    @annotate_doc_failure
    def save(self, doc, *args, **kwargs):
        data = self._prep_save(doc, kwargs.pop('validate', True))
        if args:
            values = dict((arg, data[arg]) for arg in args)
            result = self._impl(doc).update(
                dict(_id=doc._id), {'$set':values}, safe=kwargs.get('safe', True))
        else:
            result = self._impl(doc).save(data, safe=kwargs.get('safe', True))
        if result and '_id' not in doc:
            doc._id = result

    @annotate_doc_failure
    def insert(self, doc, **kwargs):
        data = self._prep_save(doc, kwargs.pop('validate', True))
        bson = self._impl(doc).insert(data, safe=kwargs.get('safe', True))
        if bson and '_id' not in doc:
            doc._id = bson

    @annotate_doc_failure
    def upsert(self, doc, spec_fields, **kwargs):
        self._prep_save(doc, kwargs.pop('validate', True))
        if type(spec_fields) != list:
            spec_fields = [spec_fields]
        self._impl(doc).update(dict((k,doc[k]) for k in spec_fields),
                               doc,
                               upsert=True,
                               safe=True)

    @annotate_doc_failure
    def delete(self, doc):
        self._impl(doc).remove({'_id':doc._id}, safe=True)

    def _set(self, doc, key_parts, value):
        if len(key_parts) == 0:
            return
        elif len(key_parts) == 1:
            doc[key_parts[0]] = value
        else:
            self._set(doc[key_parts[0]], key_parts[1:], value)

    @annotate_doc_failure
    def set(self, doc, fields_values):
        """
        sets a key/value pairs, and persists those changes to the datastore
        immediately
        """
        fields_values = Object.from_bson(fields_values)
        fields_values.make_safe()
        for k,v in fields_values.iteritems():
            self._set(doc, k.split('.'), v)
        impl = self._impl(doc)
        impl.update({'_id':doc._id}, {'$set':fields_values}, safe=True)

    @annotate_doc_failure
    def increase_field(self, doc, **kwargs):
        """
        usage: increase_field(key=value)
        Sets a field to value, only if value is greater than the current value
        Does not change it locally
        """
        key = kwargs.keys()[0]
        value = kwargs[key]
        if value is None:
            raise ValueError, "%s=%s" % (key, value)

        if key not in doc:
            self._impl(doc).update(
                {'_id': doc._id, key: None},
                {'$set': {key: value}},
                safe = True,
            )
        self._impl(doc).update(
            {'_id': doc._id, key: {'$lt': value}},
            # failed attempt at doing it all in one operation
            #{'$where': "this._id == '%s' && (!(%s in this) || this.%s < '%s')"
            #    % (doc._id, key, key, value)},
            {'$set': {key: value}},
            safe = True,
        )

    def index_information(self, cls):
        return self._impl(cls).index_information()

    def drop_indexes(self, cls):
        try:
            return self._impl(cls).drop_indexes()
        except:
            pass
