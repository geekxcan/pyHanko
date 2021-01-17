
"""
Utilities for writing PDF files.
Contains code from the PyPDF2 project; see :ref:`here <pypdf2-license>`
for the original license.
"""

import os
import struct
from io import BytesIO
from typing import List, Union, Optional, Tuple

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.crypt import SecurityHandler, StandardSecurityHandler
from pyhanko.pdf_utils.generic import pdf_name, pdf_string
from pyhanko.pdf_utils.misc import (
    peek, PdfReadError, instance_test,
    PdfWriteError,
)
from pyhanko.pdf_utils.rw_common import PdfHandler


__all__ = [
    'ObjectStream', 'BasePdfFileWriter',
    'PageObject', 'PdfFileWriter', 'init_xobject_dictionary'
]

# TODO include version number
VENDOR = 'pyhanko'


OBJSTREAM_FORBIDDEN = (generic.IndirectObject, generic.StreamObject)


class ObjectStream:
    """
    Utility class to collect objects into a PDF object stream.

    Object streams are mainly useful for space efficiency reasons.
    They allow related objects to be grouped & compressed together in a
    more flexible manner.


    .. warning::
        Object streams can only be used in files with a cross-reference
        stream, as opposed to a classical XRef table.
        In particular, this means that incremental updates to files with a
        legacy XRef table cannot contain object streams either.
        See § 7.5.7 in ISO 32000-1 for further details.

    .. warning::
        The usefulness of object streams is somewhat stymied by the fact that
        PDF stream objects cannot be embedded into object streams for
        syntactical reasons.
    """

    def __init__(self, compress=True):
        self._obj_refs = {}
        self.compress = compress

    def add_object(self, idnum: int, obj: generic.PdfObject):
        """
        Add an object to an object stream.
        Note that objects in object streams always have their generation number
        set to `0` by definition.

        :param idnum:
            The object's ID number.
        :param obj:
            The object to embed into the object stream.
        :raise TypeError:
            Raised if ``obj`` is an instance of :class:`~.generic.StreamObject`
            or :class:`~.generic.IndirectObject`.
        """

        if isinstance(obj, OBJSTREAM_FORBIDDEN):
            raise TypeError(
                'Stream objects and bare references cannot be embedded into '
                'object streams.'
            )  # pragma: nocover
        self._obj_refs[idnum] = obj

    def as_pdf_object(self) -> generic.StreamObject:
        """
        Render the object stream to a PDF stream object

        :return: An instance of :class:`~.generic.StreamObject`.
        """
        stream_header = BytesIO()
        main_body = BytesIO()
        for idnum, obj in self._obj_refs.items():
            offset = main_body.tell()
            obj.write_to_stream(main_body, None)
            stream_header.write(b'%d %d ' % (idnum, offset))

        first_obj_offset = stream_header.tell()
        stream_header.seek(0)
        sh_bytes = stream_header.read(first_obj_offset)
        stream_data = sh_bytes + main_body.getvalue()
        stream_object = generic.StreamObject({
            pdf_name('/Type'): pdf_name('/ObjStm'),
            pdf_name('/N'): generic.NumberObject(len(self._obj_refs)),
            pdf_name('/First'): generic.NumberObject(first_obj_offset)
        }, stream_data=stream_data)
        if self.compress:
            stream_object.compress()
        return stream_object


def _contiguous_xref_chunks(position_dict):
    """
    Helper method to divide the XRef table (or stream) into contiguous chunks.
    """
    previous_idnum = None
    current_chunk = []

    # iterate over keys in object ID order
    key_iter = sorted(position_dict.keys(), key=lambda t: t[1])
    (_, first_idnum), key_iter = peek(key_iter)
    for ix in key_iter:
        generation, idnum = ix

        # the idnum jumped, so yield the current chunk
        # and start a new one
        if current_chunk and idnum != previous_idnum + 1:
            yield first_idnum, current_chunk
            current_chunk = []
            first_idnum = idnum

        # append the object reference to the current chunk
        # (xref table requires position and generation entries)
        current_chunk.append((position_dict[ix], generation))
        previous_idnum = idnum

    # there is always at least one chunk, so this is fine
    yield first_idnum, current_chunk


def _write_xref_table(stream, position_dict):
    xref_location = stream.tell()
    stream.write(b'xref\n')
    # Insert xref table subsections in contiguous chunks.
    # This is necessarily more complicated than the implementation
    # in PyPDF2 (see ISO 32000 § 7.5.4, esp. on updates), since
    # we need to handle incremental updates correctly.
    subsections = _contiguous_xref_chunks(position_dict)

    def write_header(idnum, length):
        header = '%d %d\n' % (idnum, length)
        stream.write(header.encode('ascii'))

    def write_subsection(chunk):
        for position, generation in chunk:
            entry = "%010d %05d n \n" % (position, generation)
            stream.write(entry.encode('ascii'))

    first_idnum, subsection = next(subsections)
    # TODO support deleting objects
    # case distinction: in contrast with the above we have to ensure that
    # everything is written in one chunk when *not* doing incremental updates.
    # In particular, this applies to the null object
    null_obj_ref = b'0000000000 65535 f \n'
    if first_idnum == 1:
        # integrate the null object into the first subsection
        write_header(0, len(subsection) + 1)
        stream.write(null_obj_ref)
        write_subsection(subsection)
    else:
        # insert origin of linked list of freed objects, and then the first
        # subsection, as usual
        stream.write(b'0 1\n')
        stream.write(null_obj_ref)
        write_header(first_idnum, len(subsection))
        write_subsection(subsection)
    for first_idnum, subsection in subsections:
        # subsection header: list first object ID + length of subsection
        write_header(first_idnum, len(subsection))
        write_subsection(subsection)

    return xref_location


class XRefStream(generic.StreamObject):

    def __init__(self, position_dict):
        super().__init__()
        self.position_dict = position_dict

        # type indicator is one byte wide
        # we use longs to indicate positions of objects (>Q)
        # two more bytes for the generation number of an uncompressed object
        widths = map(generic.NumberObject, (1, 8, 2))
        self.update({
            pdf_name('/W'): generic.ArrayObject(widths),
            pdf_name('/Type'): pdf_name('/XRef'),
        })

    def write_to_stream(self, stream, handler=None, container_ref=None):
        # the caller is responsible for making sure that the stream
        # is registered in the position dictionary

        index = [0, 1]
        subsections = _contiguous_xref_chunks(self.position_dict)
        stream_content = BytesIO()
        # write null object
        stream_content.write(b'\x00' * 9 + b'\xff\xff')
        for first_idnum, subsection in subsections:
            index += [first_idnum, len(subsection)]
            for position, generation in subsection:
                if isinstance(position, tuple):
                    # reference to object in object stream
                    assert generation == 0
                    obj_stream_num, ix = position
                    stream_content.write(b'\x02')
                    stream_content.write(struct.pack('>Q', obj_stream_num))
                    stream_content.write(struct.pack('>H', ix))
                else:
                    stream_content.write(b'\x01')
                    stream_content.write(struct.pack('>Q', position))
                    stream_content.write(struct.pack('>H', generation))
        index_entry = generic.ArrayObject(map(generic.NumberObject, index))

        self[pdf_name('/Index')] = index_entry
        self._data = stream_content.getbuffer()
        super().write_to_stream(stream, None)


# TODO move this to content.py?
def init_xobject_dictionary(command_stream: bytes, box_width, box_height,
                            resources: Optional[generic.DictionaryObject]
                            = None) -> generic.StreamObject:
    """
    Helper function to initialise form XObject dictionaries.

    .. note::
        For utilities to handle image XObjects, see :mod:`.images`.

    :param command_stream:
        The XObject's raw appearance stream.
    :param box_width:
        The width of the XObject's bounding box.
    :param box_height:
        The height of the XObject's bounding box.
    :param resources:
        A resource dictionary to include with the form object.
    :return:
        A :class:`~.generic.StreamObject` representation of the form XObject.
    """
    resources = resources or generic.DictionaryObject()
    return generic.StreamObject({
        pdf_name('/BBox'): generic.ArrayObject(list(
            map(generic.FloatObject, (0.0, box_height, box_width, 0.0))
        )),
        pdf_name('/Resources'): resources,
        pdf_name('/Type'): pdf_name('/XObject'),
        pdf_name('/Subtype'): pdf_name('/Form')
    }, stream_data=command_stream)


class BasePdfFileWriter(PdfHandler):
    """Base class for PDF writers."""

    output_version = (1, 7)
    """Output version to be declared in the output file."""

    stream_xrefs: bool
    """
    Boolean controlling whether or not the output file will contain 
    its cross-references in stream format, or as a classical XRef table.
    
    The default for new files is ``True``. For incremental updates,
    the writer adapts to the system used in the previous iteration of the
    document (as mandated by the standard).
    """

    def __init__(self, root, info, document_id, obj_id_start=0,
                 stream_xrefs=True):
        self.objects = {}
        self.object_streams: List[ObjectStream] = list()
        self.objs_in_streams = {}
        self._lastobj_id = obj_id_start
        self._resolves_objs_from = (self,)
        self._allocated_placeholders = set()

        if isinstance(root, generic.IndirectObject):
            self._root = root
        else:
            self._root = self.add_object(root)

        self.security_handler: Optional[SecurityHandler] = None
        self._encrypt = self._encrypt_key = None
        self._document_id = document_id
        self.stream_xrefs = stream_xrefs
        if info is not None and \
                not isinstance(info, generic.IndirectObject):
            self._info = self.add_object(info)
        else:
            self._info = info

    def set_info(self, info: Optional[Union[generic.IndirectObject,
                                      generic.DictionaryObject]]):
        """
        Set the ``/Info`` entry of the document trailer.

        :param info:
            The new ``/Info`` dictionary, either as an indirect reference
            or as a :class:`~.generic.DictionaryObject`
        """
        if info is not None and \
                not isinstance(info, generic.IndirectObject):
            self._info = info = self.add_object(info)
        else:
            self._info = info
        return info

    def document_id(self) -> Tuple[bytes, bytes]:
        id_arr = self._document_id
        return id_arr[0].original_bytes, id_arr[1].original_bytes

    def mark_update(self, obj_ref: Union[generic.Reference,
                                         generic.IndirectObject]):
        """
        Mark an object reference to be updated.
        This is only relevant for incremental updates, but is included
        as a no-op by default for interoperability reasons.

        :param obj_ref:
            An indirect object instance or a reference.
        """
        pass

    def update_container(self, obj: generic.PdfObject):
        """
        Mark the container of an object (as indicated by the
        :attr:`~.generic.PdfObject.container_ref` attribute on
        :class:`~.generic.PdfObject`) for an update.

        As with :meth:`mark_update`, this only applies to incremental updates,
        but defaults to a no-op.

        :param obj:
            The object whose top-level container needs to be rewritten.
        """
        pass

    @property
    def root_ref(self) -> generic.Reference:
        return self._root.reference

    def get_object(self, ido):
        if ido.pdf not in self._resolves_objs_from:
            raise ValueError(
                f'Reference {ido} has no relation to this PDF writer.'
            )
        idnum = ido.idnum
        generation = ido.generation
        try:
            return self.objects[(generation, idnum)]
        except KeyError:
            if generation == 0:
                if idnum in self._allocated_placeholders:
                    return generic.NullObject()
                try:
                    return self.objs_in_streams[idnum]
                except KeyError:
                    pass
            raise KeyError(ido)

    def allocate_placeholder(self) -> generic.IndirectObject:
        """
        Allocate an object reference to populate later.
        Calls to :meth:`get_object` for this reference will
        return :class:`~.generic.NullObject` until it is populated using
        :meth:`add_object`.

        This method is only relevant in certain advanced contexts where
        an object ID needs to be known before the object it refers
        to can be built; chances are you'll never need it.

        :return:
            A :class:`~.generic.IndirectObject` instance referring to
            the object just allocated.
        """

        idnum = self._lastobj_id + 1
        self._allocated_placeholders.add(idnum)
        self._lastobj_id += 1
        return generic.IndirectObject(idnum, 0, self)

    def add_object(self, obj, obj_stream: Optional[ObjectStream] = None,
                   idnum=None) -> generic.IndirectObject:
        """
        Add a new object to this writer.

        :param obj:
            The object to add.
        :param obj_stream:
            An object stream to add the object to.
        :param idnum:
            Manually specify the object ID of the object to be added.
            This is only allowed for object IDs that have previously been
            allocated using :meth:`allocate_placeholder`.
        :return:
            A :class:`~.generic.IndirectObject` instance referring to
            the object just added.
        """

        if idnum is not None:
            if idnum not in self._allocated_placeholders:
                raise PdfWriteError(
                    "Manually specifying idnum is only allowed for "
                    "references previously allocated using "
                    "allocate_placeholder()."
                )
            preallocated = True
        else:
            preallocated = False
            idnum = self._lastobj_id + 1

        if obj_stream is None:
            self.objects[(0, idnum)] = obj
        elif obj_stream in self.object_streams:
            obj_stream.add_object(idnum, obj)
            self.objs_in_streams[idnum] = obj
        else:
            raise PdfWriteError(
                f'Stream {repr(obj_stream)} is unknown to this PDF writer.'
            )

        if preallocated:
            self._allocated_placeholders.remove(idnum)
        else:
            self._lastobj_id += 1
        return generic.IndirectObject(idnum, 0, self)

    def prepare_object_stream(self, compress=True):
        """Prepare and return a new :class:`.ObjectStream` object.

        :param compress:
            Indicates whether the resulting object stream should be compressed.
        :return:
            An :class:`.ObjectStream` object.
        """
        if not self.stream_xrefs:  # pragma: no cover
            raise PdfWriteError(
                'Object streams require Xref streams to be enabled.'
            )
        stream = ObjectStream(compress=compress)
        self.object_streams.append(stream)
        return stream

    def _write_header(self, stream):
        pass

    def _assign_security_handler(self, sh: SecurityHandler):
        self.security_handler = sh
        self._encrypt = self.add_object(sh.as_pdf_object())

    def _write_objects(self, stream, object_position_dict):
        # deal with objects in object streams first
        for obj_stream in self.object_streams:
            # first, register the object stream object
            #  (will get written later)
            stream_ref = self.add_object(obj_stream.as_pdf_object())
            # loop over all objects in the stream, and prepare
            # the data to put in the XRef table
            for ix, (idnum, obj) in enumerate(obj_stream._obj_refs.items()):
                object_position_dict[(0, idnum)] = (stream_ref.idnum, ix)

        for ix in sorted(self.objects.keys()):
            generation, idnum = ix
            obj = self.objects[ix]
            object_position_dict[ix] = stream.tell()
            stream.write(('%d %d obj\n' % (idnum, generation)).encode('ascii'))
            if self.security_handler is not None \
                    and idnum != self._encrypt.idnum:
                handler = self.security_handler
            else:
                handler = None
            container_ref = generic.Reference(idnum, generation, self)
            obj.write_to_stream(stream, handler, container_ref)
            stream.write(b'\nendobj\n')

    def _populate_trailer(self, trailer):
        # prepare trailer dictionary entries
        trailer[pdf_name('/Root')] = self._root
        if self._info is not None:
            trailer[pdf_name('/Info')] = self._info
        if self._encrypt is not None:
            trailer[pdf_name('/Encrypt')] = self._encrypt
        # before doing anything else, we attempt to load the crypto-relevant
        # data, so that we can bail early if something's not right
        trailer[pdf_name('/ID')] = self._document_id

    @property
    def trailer_view(self) -> generic.DictionaryObject:
        trailer = generic.DictionaryObject()
        self._populate_trailer(trailer)
        return trailer

    def write(self, stream):
        """
        Write the contents of this PDF writer to a stream.

        :param stream:
            A writable output stream.
        """
        self._write(stream)

    def _write(self, stream, skip_header=False):

        object_positions = {}

        if self.stream_xrefs:
            trailer = XRefStream(object_positions)
            trailer.compress()
        else:
            trailer = generic.DictionaryObject()

        if not skip_header:
            self._write_header(stream)
        self._populate_trailer(trailer)
        self._write_objects(stream, object_positions)

        if self.stream_xrefs:
            xref_location = stream.tell()
            xrefs_id = self._lastobj_id + 1
            # add position of XRef stream to the XRef stream
            object_positions[(0, xrefs_id)] = xref_location
            trailer[pdf_name('/Size')] = generic.NumberObject(xrefs_id + 1)
            # write XRef stream
            stream.write(('%d %d obj' % (xrefs_id, 0)).encode('ascii'))
            trailer.write_to_stream(stream, None)
            stream.write(b'\nendobj\n')
        else:
            # classical xref table
            xref_location = _write_xref_table(stream, object_positions)
            trailer[pdf_name('/Size')] = generic.NumberObject(
                self._lastobj_id + 1
            )
            # write trailer
            stream.write(b'trailer\n')
            trailer.write_to_stream(stream, None)

        # write xref table pointer and EOF
        xref_pointer_string = '\nstartxref\n%s\n' % xref_location
        stream.write(xref_pointer_string.encode('ascii') + b'%%EOF\n')

    def register_annotation(self, page_ref, annot_ref):
        """
        Register an annotation to be added to a page.
        This convenience function takes care of calling :meth:`mark_update`
        where necessary.

        :param page_ref:
            Reference to the page object involved.
        :param annot_ref:
            Reference to the annotation object to be added.
        """
        page_obj = page_ref.get_object()
        try:
            annots_ref = page_obj.raw_get('/Annots')
            if isinstance(annots_ref, generic.IndirectObject):
                annots = annots_ref.get_object()
                self.mark_update(annot_ref)
            else:
                # we need to update the entire page object if the annots array
                # is a direct object
                annots = annots_ref
                self.mark_update(page_ref)
        except KeyError:
            annots = generic.ArrayObject()
            self.mark_update(page_ref)
            page_obj[pdf_name('/Annots')] = annots

        annots.append(annot_ref)

    def insert_page(self, new_page, after=None):
        """
        Insert a page object into the tree.

        :param new_page:
            Page object to insert.
        :param after:
            Page number (zero-indexed) after which to insert the page.
        :return:
            A reference to the newly inserted page.
        """
        if new_page['/Type'] != pdf_name('/Page'):
            raise ValueError('Not a page object')
        if '/Parent' in new_page:
            raise ValueError('/Parent must not be set.')

        page_tree_root_ref = self.root.raw_get('/Pages')
        if after is None:
            page_count = page_tree_root_ref.get_object()['/Count']
            after = page_count - 1

        if after == -1:
            # there are no pages yet, this will be the first
            pages_obj_ref = page_tree_root_ref
            kid_ix = -1
        else:
            pages_obj_ref, kid_ix, _ = self.find_page_container(after)

        pages_obj = pages_obj_ref.get_object()
        try:
            kids = pages_obj['/Kids']
        except KeyError:  # pragma: nocover
            raise ValueError('/Pages must have /Kids')

        # increase page count for all parents
        parent = pages_obj
        while parent is not None:
            # can't use += 1 because of the way PyPDF2's generic types work
            count = parent['/Count']
            parent[pdf_name('/Count')] = generic.NumberObject(count + 1)
            parent = parent.get('/Parent')
        new_page_ref = self.add_object(new_page)
        kids.insert(kid_ix + 1, new_page_ref)
        new_page[pdf_name('/Parent')] = pages_obj_ref
        self.update_container(pages_obj)
        self.update_container(kids)

        return new_page_ref

    def import_object(self, obj: generic.PdfObject,
                      obj_stream: Optional[ObjectStream] = None) \
            -> generic.PdfObject:
        """
        Deep-copy an object into this writer, dealing with resolving indirect
        references in the process.

        :param obj:
            The object to import.
        :param obj_stream:
            The object stream to import objects into.

            .. note::
                Stream objects and bare references will not be put into
                the object stream; the standard forbids this.
        :return:
            The object as associated with this writer.
            If the input object was an indirect reference, a dictionary
            (incl. streams) or an array, the returned value will always be
            a new instance.
        """

        return self._import_object(obj, {}, obj_stream)

    def _import_object(self, obj: generic.PdfObject, reference_map: dict,
                       obj_stream) -> generic.PdfObject:

        # TODO check the spec for guidance on fonts. Do font identifiers have
        #  to be globally unique?

        # TODO deal with container_ref

        if isinstance(obj, generic.DecryptedObjectProxy):
            obj = obj.decrypted
        if isinstance(obj, generic.IndirectObject):
            try:
                return reference_map[obj.reference]
            except KeyError:
                refd = obj.get_object()
                # Add a placeholder to reserve the reference value.
                # This ensures correct behaviour in recursive calls
                # with self-references.
                new_ido = self.allocate_placeholder()
                reference_map[obj.reference] = new_ido
                imported = self._import_object(refd, reference_map, obj_stream)

                # if the imported object is a bare reference and/or a stream
                # object, we can't put it into an object stream.
                if isinstance(imported, OBJSTREAM_FORBIDDEN):
                    obj_stream = None

                # fill in the placeholder
                self.add_object(
                    imported, obj_stream=obj_stream, idnum=new_ido.idnum
                )
                return new_ido
        elif isinstance(obj, generic.DictionaryObject):
            raw_dict = {
                k: self._import_object(v, reference_map, obj_stream)
                for k, v in obj.items()
            }
            if isinstance(obj, generic.StreamObject):
                # In the vast majority of use cases, I'd expect the content
                # to be available in encoded form by default.
                # By initialising the stream object in this way, we avoid
                # a potentially costly decoding operation.
                return generic.StreamObject(
                    raw_dict, encoded_data=obj.encoded_data
                )
            else:
                return generic.DictionaryObject(raw_dict)
        elif isinstance(obj, generic.ArrayObject):
            return generic.ArrayObject(
                self._import_object(v, reference_map, obj_stream) for v in obj
            )
        else:
            return obj

    def import_page_as_xobject(self, other: PdfHandler, page_ix=0,
                               content_stream=0, inherit_filters=True):
        """
        Import a page content stream from some other
        :class:`~.rw_common.PdfHandler` into the current one as a form XObject.

        :param other:
            A :class:`~.rw_common.PdfHandler`
        :param page_ix:
            Index of the page to copy (default: 0)
        :param content_stream:
            Index of the page's content stream to copy, if multiple are present
            (default: 0)
        :param inherit_filters:
            Inherit the content stream's filters, if present.
        :return:
            An :class:`~.generic.IndirectObject` referring to the page object
            as added to the current reader.
        """
        page_ref, resources = other.find_page_for_modification(page_ix)
        page_obj = page_ref.get_object()

        # find the page's /MediaBox by going up the tree until we encounter it
        pagetree_obj = page_obj
        while True:
            try:
                mb = pagetree_obj['/MediaBox']
                break
            except KeyError:
                try:
                    pagetree_obj = pagetree_obj['/Parent']
                except KeyError:  # pragma: nocover
                    raise PdfReadError(
                        f'Page {page_ix} does not have a /MediaBox'
                    )

        stream_dict = {
            pdf_name('/BBox'): mb,
            pdf_name('/Resources'): self.import_object(resources),
            pdf_name('/Type'): pdf_name('/XObject'),
            pdf_name('/Subtype'): pdf_name('/Form')
        }
        command_stream = page_obj['/Contents']
        # if the page /Contents is an array, retrieve the content stream
        # with the appropriate index
        if isinstance(command_stream, generic.ArrayObject):
            command_stream = command_stream[content_stream].get_object()
        assert isinstance(command_stream, generic.StreamObject)
        filters = None
        if inherit_filters:
            try:
                # try to inherit filters from the original command stream
                filters = command_stream['/Filter']
            except KeyError:
                pass

        if filters is not None:
            stream_dict[pdf_name('/Filter')] = self.import_object(filters)
            result = generic.StreamObject(
                stream_dict, encoded_data=command_stream.encoded_data
            )
        else:
            result = generic.StreamObject(
                stream_dict, stream_data=command_stream.data
            )

        return self.add_object(result)


class PageObject(generic.DictionaryObject):
    """Subclass of :class:`~.generic.DictionaryObject` that handles some of the
    initialisation boilerplate for page objects."""

    # TODO be more clever with inheritable required attributes,
    #  and enforce the requirements on insertion instead
    # (setting /MediaBox at the page tree root seems to make sense, for example)
    def __init__(self, contents, media_box, resources=None):
        resources = resources or generic.DictionaryObject()

        if isinstance(contents, list):
            if not all(map(instance_test(generic.IndirectObject), contents)):
                raise PdfWriteError(
                    'Contents array must consist of indirect references'
                )
            if not isinstance(contents, generic.ArrayObject):
                contents = generic.ArrayObject(contents)
        elif not isinstance(contents, generic.IndirectObject):
            raise PdfWriteError(
                'Contents must be either an indirect reference or an array'
            )

        if len(media_box) != 4:
            raise ValueError('Media box must consist of 4 coordinates.')
        super().__init__({
            pdf_name('/Type'): pdf_name('/Page'),
            pdf_name('/MediaBox'): generic.ArrayObject(
                map(generic.NumberObject, media_box)
            ),
            pdf_name('/Resources'): resources,
            pdf_name('/Contents'): contents
        })


class PdfFileWriter(BasePdfFileWriter):
    """Class to write new PDF files."""

    def __init__(self, stream_xrefs=True, init_page_tree=True):
        # root object
        root = generic.DictionaryObject({
            pdf_name("/Type"): pdf_name("/Catalog"),
        })

        id1 = generic.ByteStringObject(os.urandom(16))
        id2 = generic.ByteStringObject(os.urandom(16))
        id_obj = generic.ArrayObject([id1, id2])

        # info object
        info = generic.DictionaryObject({
            pdf_name('/Producer'): pdf_string(VENDOR)
        })

        super().__init__(root, info, id_obj, stream_xrefs=stream_xrefs)

        if init_page_tree:
            pages = generic.DictionaryObject({
                pdf_name("/Type"): pdf_name("/Pages"),
                pdf_name("/Count"): generic.NumberObject(0),
                pdf_name("/Kids"): generic.ArrayObject(),
            })

            root[pdf_name('/Pages')] = self.add_object(pages)

    def _write_header(self, stream):
        major, minor = self.output_version
        stream.write(f'%PDF-{major}.{minor}\n'.encode('ascii'))
        # write some binary characters to make sure the file is flagged
        # as binary (see § 7.5.2 in ISO 32000-1)
        stream.write(b'%\xc2\xa5\xc2\xb1\xc3\xab\n')

    def encrypt(self, owner_pass, user_pass=None):
        """
        Mark this document to be encrypted with PDF 2.0 encryption (AES-256).

        .. caution::
            While pyHanko supports legacy PDF encryption as well, the API
            to create those is left undocumented on purpose to discourage
            its use.

            Incremental updates to a file encrypted with one of these
            outdated methods will still work, however.


        :param owner_pass:
            The desired owner password.
        :param user_pass:
            The desired user password (defaults to the owner password
            if not specified)
        """
        self.output_version = (2, 0)
        sh = StandardSecurityHandler.build_from_pw(owner_pass, user_pass)
        self._assign_security_handler(sh)
