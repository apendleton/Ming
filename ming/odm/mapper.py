from copy import copy
from ming.base import Object
from ming.utils import wordwrap

from .base import ObjectState, state, with_hooks
from .property import FieldProperty

def mapper(cls, collection=None, session=None, **kwargs):
    if collection is None and session is None:
        if isinstance(cls, type):
            return Mapper.by_class(cls)
        elif isinstance(cls, basestring):
            return Mapper.by_classname(cls)
        else:
            return Mapper._mapper_by_class[cls.__class__]
    return Mapper(cls, collection, session, **kwargs)

class Mapper(object):
    _mapper_by_collection = {}
    _mapper_by_class = {}
    _mapper_by_classname = {}
    _all_mappers = []
    _compiled = False

    def __init__(self, mapped_class, collection, session, **kwargs):
        self.mapped_class = mapped_class
        self.collection = collection
        self.session = session
        self.properties = []
        self.property_index = {}
        classname = '%s.%s' % (mapped_class.__module__, mapped_class.__name__)
        self._mapper_by_collection[collection] = self
        self._mapper_by_class[mapped_class] = self
        self._mapper_by_classname[classname] = self
        self._all_mappers.append(self)
        properties = kwargs.pop('properties', {})
        include_properties = kwargs.pop('include_properties', None)
        exclude_properties = kwargs.pop('exclude_properties', [])
        extensions = kwargs.pop('extensions', [])
        self.extensions = [e(self) for e in extensions]
        self.options = Object(kwargs.pop('options', dict(refresh=False, instrument=True)))
        if kwargs:
            raise TypeError, 'Unknown kwd args: %r' % kwargs
        self._instrument_class(properties, include_properties, exclude_properties)

    def __repr__(self):
        return '<Mapper %s:%s>' % (
            self.mapped_class.__name__, self.collection.m.collection_name)

    @with_hooks('insert')
    def insert(self, obj, state, session, **kwargs):
        doc = self.collection(state.document, skip_from_bson=True)
        session.impl.insert(doc, validate=False)
        state.status = state.clean

    @with_hooks('update')
    def update(self, obj, state, session, **kwargs):
        doc = self.collection(state.document, skip_from_bson=True)
        session.impl.save(doc, validate=False)
        state.status = state.clean

    @with_hooks('delete')
    def delete(self, obj, state, session, **kwargs):
        doc = self.collection(state.document, skip_from_bson=True)
        session.impl.delete(doc)

    @with_hooks('remove')
    def remove(self, session, *args, **kwargs):
        session.impl.remove(self.collection, *args, **kwargs)

    def create(self, doc, options):
        doc = self.collection.make(doc)
        mapper = self.by_collection(type(doc))
        return mapper._from_doc(doc, Object(self.options, **options))

    def base_mappers(self):
        for base in self.mapped_class.__bases__:
            if base in self._mapper_by_class:
                yield self._mapper_by_class[base]

    def all_properties(self):
        seen = set()
        for p in self.properties:
            if p.name in seen: continue
            seen.add(p.name)
            yield p
        for base in self.base_mappers():
            for p in base.all_properties():
                if p.name in seen: continue
                seen.add(p.name)
                yield p
                
    @classmethod
    def by_collection(cls, collection_class):
        return cls._mapper_by_collection[collection_class]

    @classmethod
    def by_class(cls, mapped_class):
        return cls._mapper_by_class[mapped_class]

    @classmethod
    def by_classname(cls, name):
        try:
            return cls._mapper_by_classname[name]
        except KeyError:
            for n, mapped_class in cls._mapper_by_classname.iteritems():
                if n.endswith('.' + name): return mapped_class
            raise

    @classmethod
    def all_mappers(cls):
        return cls._all_mappers

    @classmethod
    def compile_all(cls):
        for m in cls.all_mappers():
            m.compile()

    @classmethod
    def clear_all(cls):
        for m in cls.all_mappers():
            m._compiled = False
        cls._all_mappers = []

    def compile(self):
        if self._compiled: return
        self._compiled = True
        for p in self.properties:
            p.compile(self)
    
    def update_partial(self, session, *args, **kwargs):
        session.impl.update_partial(self.collection, *args, **kwargs)

    def _from_doc(self, doc, options):
        obj = self.mapped_class.__new__(self.mapped_class)
        obj.__ming__ = _ORMDecoration(self, obj, options)
        st = state(obj)
        st.original_document = doc
        st.document = self.collection.m.schema.validate(doc)
        st.status = st.new
        # self.session.save(obj)
        return obj

    def _instrument_class(self, properties, include_properties, exclude_properties):
        self.mapped_class.query = _QueryDescriptor(self)
        properties = dict(properties)
        # Copy properties from inherited mappers
        for b in self.base_mappers():
            for prop in b.properties:
                properties.setdefault(prop.name, copy(prop))
        # Copy default properties from collection class
        for fld in self.collection.m.fields:
            properties.setdefault(fld.name, FieldProperty(fld))
        # Handle include/exclude_properties
        if include_properties:
            properties = dict((k,properties[k]) for k in include_properties)
        for k in exclude_properties:
            properties.pop(k, None)
        for k,v in properties.iteritems():
            v.name = k
            v.mapper = self
            setattr(self.mapped_class, k, v)
            self.properties.append(v)
            self.property_index[k] = v
        _InitDecorator.decorate(self.mapped_class, self)
        inst = self._instrumentation()
        for k in ('__repr__', '__getitem__', '__setitem__', '__contains__',
                  'delete'):
            if getattr(self.mapped_class, k, ()) == getattr(object, k, ()):
                setattr(self.mapped_class, k, getattr(inst, k).im_func)

    def _instrumentation(self):
        class _Instrumentation(object):
            def __repr__(self_):
                properties = [
                    '%s=%s' % (prop.name, prop.repr(self_))
                    for prop in mapper(self_).properties
                    if prop.include_in_repr ]
                return wordwrap(
                    '<%s %s>' % 
                    (self_.__class__.__name__, ' '.join(properties)),
                    60,
                    indent_subsequent=2)
            def delete(self_):
                self_.query.delete()
            def __getitem__(self_, name):
                try:
                    return getattr(self_, name)
                except AttributeError:
                    raise KeyError, name
            def __setitem__(self_, name, value):
                setattr(self_, name, value)
            def __contains__(self_, name):
                return hasattr(self_, name)
        return _Instrumentation


class MapperExtension(object):
    """Base implementation for customizing Mapper behavior."""
    def __init__(self, mapper):
        self.mapper = mapper
    def before_insert(self, instance, state, sess):
        """Receive an object instance and its current state before that
        instance is inserted into its collection."""
        pass
    def after_insert(self, instance, state, sess):
        """Receive an object instance and its current state after that
        instance is inserted into its collection."""
        pass
    def before_update(self, instance, state, sess):
        """Receive an object instance and its current state before that
        instance is updated."""
        pass
    def after_update(self, instance, state, sess):
        """Receive an object instance and its current state after that
        instance is updated."""
        pass
    def before_delete(self, instance, state, sess):
        """Receive an object instance and its current state before that
        instance is deleted."""
        pass
    def after_delete(self, instance, state, sess):
        """Receive an object instance and its current state after that
        instance is deleted."""
    def before_remove(self, sess): pass
    def after_remove(self, sess): pass

class _ORMDecoration(object):

    def __init__(self, mapper, instance, options):
        self.mapper = mapper
        self.instance = instance
        self.state = ObjectState(options)
        self.state.document = Object()
        self.state.original_document = Object()

class _QueryDescriptor(object):

    def __init__(self, mapper):
        self.classquery = _ClassQuery(mapper)

    def __get__(self, instance, cls=None):
        if instance is None: return self.classquery
        else: return _InstQuery(self.classquery, instance)

class _ClassQuery(object):
    _proxy_methods = (
        'find', 'find_and_modify', 'remove', 'update' )

    def __init__(self, mapper):
        self.mapper = mapper
        self.session = self.mapper.session
        self.mapped_class = self.mapper.mapped_class

        def _proxy(name):
            def inner(*args, **kwargs):
                method = getattr(self.session, name)
                return method(self.mapped_class, *args, **kwargs)
            inner.__name__ = name
            return inner

        for method_name in self._proxy_methods:
            setattr(self, method_name, _proxy(method_name))

    def get(self, **kwargs):
        if kwargs.keys() == [ '_id' ]:
            return self.session.get(self.mapped_class, kwargs['_id'])
        return self.find(kwargs).first()

    def find_by(self, **kwargs):
        return self.find(kwargs)

class _InstQuery(object):
    _proxy_methods = (
        'update_if_not_modified',
        )

    def __init__(self, classquery, instance):
        self.classquery = classquery
        self.mapper = classquery.mapper
        self.session = classquery.session
        self.mapped_class = classquery.mapped_class
        self.instance = instance

        def _proxy(name):
            def inner(*args, **kwargs):
                method = getattr(self.session, name)
                return method(self.instance, *args, **kwargs)
            inner.__name__ = name
            return inner

        for method_name in self._proxy_methods:
            setattr(self, method_name, _proxy(method_name))

        # Some methods are just convenient (and safe)
        self.find = self.classquery.find
        self.get = self.classquery.get

    def delete(self):
        st = state(self.instance)
        st.status = st.deleted

    def update(self, fields, **kwargs):
        self.classquery.update(
            {'_id': self.instance._id },
            fields)

class _InitDecorator(object):

    def __init__(self, mapper, func):
        self.mapper = mapper
        self.func = func

    @property
    def schema(self):
        return self.mapper.collection.m.schema

    def saving_init(self, self_):
        def __init__(*args, **kwargs):
            self_.__ming__ = _ORMDecoration(self.mapper, self_, Object(self.mapper.options))
            self.func(self_, *args, **kwargs)
            if self.mapper.session:
                self.save(self_)
        return __init__

    def save(self, obj):
        if self.schema:
            obj.__ming__.state.validate(self.schema)
        self.mapper.session.save(obj)
    
    def nonsaving_init(self, self_):
        def __init__(*args, **kwargs):
            self.func(self_, *args, **kwargs)
        return __init__
    
    def __get__(self, self_, cls=None):
        if self_ is None: return self
        if self.mapper.mapped_class == cls:
            return self.saving_init(self_)
        else:
            return self.nonsaving_init(self_)

    @classmethod
    def decorate(cls, mapped_class, mapper):
        old_init = mapped_class.__init__
        if isinstance(old_init, cls):
            mapped_class.__init__ = cls(mapper, old_init.func)
        elif old_init is object.__init__:
            mapped_class.__init__ = cls(mapper, _basic_init)
        else:
            mapped_class.__init__ = cls(mapper, old_init)

def _basic_init(self_, **kwargs):
    for k,v in kwargs.iteritems():
        setattr(self_, k, v)
