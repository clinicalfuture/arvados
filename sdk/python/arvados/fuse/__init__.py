#
# FUSE driver for Arvados Keep
#

import os
import sys

import llfuse
import errno
import stat
import threading
import arvados
import pprint
import arvados.events
import re

from time import time
from llfuse import FUSEError

class File(object):
    '''Wraps a StreamFileReader for use by Directory.'''

    def __init__(self, parent_inode):
        self.inode = None
        self.parent_inode = parent_inode

    def size(self):
        return 0

    def readfrom(self, off, size)):
        return ''


class StreamReaderFile(File):
    '''Wraps a StreamFileReader as a file.'''

    def __init__(self, parent_inode, reader):
        super(StreamReaderFile, self).__init__(parent_inode, reader)
        self.reader = reader

    def size(self):
        return self.reader.size()

    def readfrom(self, off, size)):
        return self.reader.readfrom(off, size)


class ObjectFile(File):
    '''Wraps a serialized object as a file.'''

    def __init__(self, parent_inode, contents):
        super(ObjectFile, self).__init__(parent_inode, reader)
        self.contents = contents

    def size(self):
        return len(self.contents)

    def readfrom(self, off, size):
        return self.contents[off:(off+size)]


class Directory(object):
    '''Generic directory object, backed by a dict.
    Consists of a set of entries with the key representing the filename
    and the value referencing a File or Directory object.
    '''

    def __init__(self, parent_inode):
        '''parent_inode is the integer inode number'''
        self.inode = None
        if not isinstance(parent_inode, int):
            raise Exception("parent_inode should be an int")
        self.parent_inode = parent_inode
        self._entries = {}
        self._stale = True
        self._poll = False
        self._last_update = time()
        self._poll_time = 60

    #  Overriden by subclasses to implement logic to update the entries dict
    #  when the directory is stale
    def update(self):
        pass

    # Mark the entries dict as stale
    def invalidate(self):
        self._stale = True

    # Test if the entries dict is stale
    def stale(self):
        if self._stale:
            return True
        if self._poll:
            return (self._last_update + self._poll_time) < time()
        return False

    def fresh(self):
        self._stale = False
        self._last_update = time()

    # Only used when computing the size of the disk footprint of the directory
    # (stub)
    def size(self):
        return 0

    def __getitem__(self, item):
        if self.stale():
            self.update()
        return self._entries[item]

    def items(self):
        if self.stale():
            self.update()
        return self._entries.items()

    def __iter__(self):
        if self.stale():
            self.update()
        return self._entries.iterkeys()

    def __contains__(self, k):
        if self.stale():
            self.update()
        return k in self._entries


class CollectionDirectory(Directory):
    '''Represents the root of a directory tree holding a collection.'''

    def __init__(self, parent_inode, inodes, collection_locator):
        super(CollectionDirectory, self).__init__(parent_inode)
        self.inodes = inodes
        self.collection_locator = collection_locator

    def update(self):
        collection = arvados.CollectionReader(arvados.Keep.get(self.collection_locator))
        for s in collection.all_streams():
            cwd = self
            for part in s.name().split('/'):
                if part != '' and part != '.':
                    if part not in cwd._entries:
                        cwd._entries[part] = self.inodes.add_entry(Directory(cwd.inode))
                    cwd = cwd._entries[part]
            for k, v in s.files().items():
                cwd._entries[k] = self.inodes.add_entry(StreamReaderFile(cwd.inode, v))
        self.fresh()


class MagicDirectory(Directory):
    '''A special directory that logically contains the set of all extant keep
    locators.  When a file is referenced by lookup(), it is tested to see if it
    is a valid keep locator to a manifest, and if so, loads the manifest
    contents as a subdirectory of this directory with the locator as the
    directory name.  Since querying a list of all extant keep locators is
    impractical, only collections that have already been accessed are visible
    to readdir().
    '''

    def __init__(self, parent_inode, inodes):
        super(MagicDirectory, self).__init__(parent_inode)
        self.inodes = inodes

    def __contains__(self, k):
        if k in self._entries:
            return True
        try:
            if arvados.Keep.get(k):
                return True
            else:
                return False
        except Exception as e:
            #print 'exception keep', e
            return False

    def __getitem__(self, item):
        if item not in self._entries:
            self._entries[item] = self.inodes.add_entry(CollectionDirectory(self.inode, self.inodes, item))
        return self._entries[item]


class TagsDirectory(Directory):
    '''A special directory that contains as subdirectories all tags visible to the user.'''

    def __init__(self, parent_inode, inodes, api, poll_time=60):
        super(TagsDirectory, self).__init__(parent_inode)
        self.inodes = inodes
        self.api = api
        try:
            arvados.events.subscribe(self.api, [['object_uuid', 'is_a', 'arvados#link']], lambda ev: self.invalidate())
        except:
            self._poll = True
            self._poll_time = poll_time

    def invalidate(self):
        with llfuse.lock:
            super(TagsDirectory, self).invalidate()
            for a in self._entries:
                self._entries[a].invalidate()

    def update(self):
        tags = self.api.links().list(filters=[['link_class', '=', 'tag']], select=['name'], distinct = True).execute()
        oldentries = self._entries
        self._entries = {}
        for n in tags['items']:
            n = n['name']
            if n in oldentries:
                self._entries[n] = oldentries[n]
            else:
                self._entries[n] = self.inodes.add_entry(TagDirectory(self.inode, self.inodes, self.api, n, poll=self._poll, poll_time=self._poll_time))
        self.fresh()


class TagDirectory(Directory):
    '''A special directory that contains as subdirectories all collections visible
    to the user that are tagged with a particular tag.
    '''

    def __init__(self, parent_inode, inodes, api, tag, poll=False, poll_time=60):
        super(TagDirectory, self).__init__(parent_inode)
        self.inodes = inodes
        self.api = api
        self.tag = tag
        self._poll = poll
        self._poll_time = poll_time

    def update(self):
        collections = self.api.links().list(filters=[['link_class', '=', 'tag'],
                                               ['name', '=', self.tag],
                                               ['head_uuid', 'is_a', 'arvados#collection']],
                                      select=['head_uuid']).execute()
        oldentries = self._entries
        self._entries = {}
        for c in collections['items']:
            n = c['head_uuid']
            if n in oldentries:
                self._entries[n] = oldentries[n]
            else:
                self._entries[n] = self.inodes.add_entry(CollectionDirectory(self.inode, self.inodes, n))
        self.fresh()


class GroupsDirectory(Directory):
    '''A special directory that contains as subdirectories all groups visible to the user.'''

    def __init__(self, parent_inode, inodes, api, poll_time=60):
        super(GroupsDirectory, self).__init__(parent_inode)
        self.inodes = inodes
        self.api = api
        try:
            arvados.events.subscribe(self.api, [], lambda ev: self.invalidate())
        except:
            self._poll = True
            self._poll_time = poll_time

    def invalidate(self):
        with llfuse.lock:
            super(GroupsDirectory, self).invalidate()
            for a in self._entries:
                self._entries[a].invalidate()

    def update(self):
        groups = self.api.groups().list().execute()
        oldentries = self._entries
        self._entries = {}
        for n in groups['items']:
            id = n['name']
            if id in oldentries and oldentries[id].uuid == n['uuid']:
                self._entries[id] = oldentries[id]
            else:
                self._entries[id] = self.inodes.add_entry(GroupDirectory(self.inode, self.inodes, self.api,
                                                                         n['uuid'], poll=self._poll, poll_time=self._poll_time))
        self.fresh()


class GroupDirectory(Directory):
    '''A special directory that contains the contents of a group.'''

    def __init__(self, parent_inode, inodes, api, uuid, poll=False, poll_time=60):
        super(GroupDirectory, self).__init__(parent_inode)
        self.inodes = inodes
        self.api = api
        self.uuid = uuid
        self._poll = poll
        self._poll_time = poll_time

    def invalidate(self):
        with llfuse.lock:
            super(GroupDirectory, self).invalidate()
            for a in self._entries:
                self._entries[a].invalidate()

    def createDirectory(self, parent_inode, inodes, api, uuid, poll, poll_time):
        print uuid
        if re.match(r'[0-9a-f]{32}\+\d+', i['uuid']):
            return CollectionDirectory(parent_inode, inodes, i['uuid'])
        if re.match(r'[a-z0-9]{5}-[a-z0-9]{5}-[a-z0-9]{15}', i['uuid']):
            return ObjectFile(parent_inode, inodes, json.dumps(i))
        return None

    def update(self):
        contents = self.api.groups().contents(uuid=self.uuid).execute()
        links = {}
        for a in contents['links']:
            links[a['head_uuid']] = a['name']

        oldentries = self._entries
        self._entries = {}

        for i in contents['items']:
            if i['uuid'] in links:
                n = links[i['uuid']]
            elif 'name' in i and len(i['name']) > 0:
                n = i['name']
            else:
                n = i['uuid']

            if n in oldentries and oldentries[n].uuid == i['uuid']:
                self._entries[n] = oldentries[n]
            else:
                d = self.createDirectory(self.inode, self.inodes, self.api, i, self._poll, self._poll_time)
                if d != None:
                    self._entries[n] = self.inodes.add_entry(d)
        self.fresh()


class FileHandle(object):
    '''Connects a numeric file handle to a File or Directory object that has
    been opened by the client.'''

    def __init__(self, fh, entry):
        self.fh = fh
        self.entry = entry


class Inodes(object):
    '''Manage the set of inodes.  This is the mapping from a numeric id
    to a concrete File or Directory object'''

    def __init__(self):
        self._entries = {}
        self._counter = llfuse.ROOT_INODE

    def __getitem__(self, item):
        return self._entries[item]

    def __setitem__(self, key, item):
        self._entries[key] = item

    def __iter__(self):
        return self._entries.iterkeys()

    def items(self):
        return self._entries.items()

    def __contains__(self, k):
        return k in self._entries

    def add_entry(self, entry):
        entry.inode = self._counter
        self._entries[entry.inode] = entry
        self._counter += 1
        return entry

class Operations(llfuse.Operations):
    '''This is the main interface with llfuse.  The methods on this object are
    called by llfuse threads to service FUSE events to query and read from
    the file system.

    llfuse has its own global lock which is acquired before calling a request handler,
    so request handlers do not run concurrently unless the lock is explicitly released
    with llfuse.lock_released.'''

    def __init__(self, uid, gid):
        super(Operations, self).__init__()

        self.inodes = Inodes()
        self.uid = uid
        self.gid = gid

        # dict of inode to filehandle
        self._filehandles = {}
        self._filehandles_counter = 1

        # Other threads that need to wait until the fuse driver
        # is fully initialized should wait() on this event object.
        self.initlock = threading.Event()

    def init(self):
        # Allow threads that are waiting for the driver to be finished
        # initializing to continue
        self.initlock.set()

    def access(self, inode, mode, ctx):
        return True

    def getattr(self, inode):
        e = self.inodes[inode]

        entry = llfuse.EntryAttributes()
        entry.st_ino = inode
        entry.generation = 0
        entry.entry_timeout = 300
        entry.attr_timeout = 300

        entry.st_mode = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
        if isinstance(e, Directory):
            entry.st_mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH | stat.S_IFDIR
        else:
            entry.st_mode |= stat.S_IFREG

        entry.st_nlink = 1
        entry.st_uid = self.uid
        entry.st_gid = self.gid
        entry.st_rdev = 0

        entry.st_size = e.size()

        entry.st_blksize = 1024
        entry.st_blocks = e.size()/1024
        if e.size()/1024 != 0:
            entry.st_blocks += 1
        entry.st_atime = 0
        entry.st_mtime = 0
        entry.st_ctime = 0

        return entry

    def lookup(self, parent_inode, name):
        #print "lookup: parent_inode", parent_inode, "name", name
        inode = None

        if name == '.':
            inode = parent_inode
        else:
            if parent_inode in self.inodes:
                p = self.inodes[parent_inode]
                if name == '..':
                    inode = p.parent_inode
                elif name in p:
                    inode = p[name].inode

        if inode != None:
            return self.getattr(inode)
        else:
            raise llfuse.FUSEError(errno.ENOENT)

    def open(self, inode, flags):
        if inode in self.inodes:
            p = self.inodes[inode]
        else:
            raise llfuse.FUSEError(errno.ENOENT)

        if (flags & os.O_WRONLY) or (flags & os.O_RDWR):
            raise llfuse.FUSEError(errno.EROFS)

        if isinstance(p, Directory):
            raise llfuse.FUSEError(errno.EISDIR)

        fh = self._filehandles_counter
        self._filehandles_counter += 1
        self._filehandles[fh] = FileHandle(fh, p)
        return fh

    def read(self, fh, off, size):
        #print "read", fh, off, size
        if fh in self._filehandles:
            handle = self._filehandles[fh]
        else:
            raise llfuse.FUSEError(errno.EBADF)

        try:
            with llfuse.lock_released:
                return handle.entry.readfrom(off, size)
        except:
            raise llfuse.FUSEError(errno.EIO)

    def release(self, fh):
        if fh in self._filehandles:
            del self._filehandles[fh]

    def opendir(self, inode):
        #print "opendir: inode", inode

        if inode in self.inodes:
            p = self.inodes[inode]
        else:
            raise llfuse.FUSEError(errno.ENOENT)

        if not isinstance(p, Directory):
            raise llfuse.FUSEError(errno.ENOTDIR)

        fh = self._filehandles_counter
        self._filehandles_counter += 1
        if p.parent_inode in self.inodes:
            parent = self.inodes[p.parent_inode]
        else:
            raise llfuse.FUSEError(errno.EIO)

        self._filehandles[fh] = FileHandle(fh, [('.', p), ('..', parent)] + list(p.items()))
        return fh

    def readdir(self, fh, off):
        #print "readdir: fh", fh, "off", off

        if fh in self._filehandles:
            handle = self._filehandles[fh]
        else:
            raise llfuse.FUSEError(errno.EBADF)

        #print "handle.entry", handle.entry

        e = off
        while e < len(handle.entry):
            yield (handle.entry[e][0], self.getattr(handle.entry[e][1].inode), e+1)
            e += 1

    def releasedir(self, fh):
        del self._filehandles[fh]

    def statfs(self):
        st = llfuse.StatvfsData()
        st.f_bsize = 1024 * 1024
        st.f_blocks = 0
        st.f_files = 0

        st.f_bfree = 0
        st.f_bavail = 0

        st.f_ffree = 0
        st.f_favail = 0

        st.f_frsize = 0
        return st

    # The llfuse documentation recommends only overloading functions that
    # are actually implemented, as the default implementation will raise ENOSYS.
    # However, there is a bug in the llfuse default implementation of create()
    # "create() takes exactly 5 positional arguments (6 given)" which will crash
    # arv-mount.
    # The workaround is to implement it with the proper number of parameters,
    # and then everything works out.
    def create(self, p1, p2, p3, p4, p5):
        raise llfuse.FUSEError(errno.EROFS)
