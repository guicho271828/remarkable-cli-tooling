#!/usr/bin/env python3

import sys
import os
import time
import datetime
import json
import shutil
import argparse
import uuid
import subprocess
import tempfile
import pathlib
import urllib.request
import re
import io
import tqdm
import ipaddress

default_prepdir = tempfile.mkdtemp(prefix="resync")

ssh_socketfile = '/tmp/remarkable-push.socket'

xochitl_dir = '~/.local/share/remarkable/xochitl'

parser = argparse.ArgumentParser(description='Push and pull files to and from your reMarkable',
                                 formatter_class=argparse.RawTextHelpFormatter)

parser.add_argument('-n', '--dry-run', dest='dryrun', action='store_true', default=False,
                    help="Don't actually copy files, just show what would be copied")
parser.add_argument('-o', '--output', action='store', default=None, dest='destination',
                    help=('Destination for copied files.'
                          '\n  In the push mode, it specifies a folder on the device and defaults to the name of the source directory.'
                          '\n  In the pull mode, it specifies a directory on the computer and defaults to the current directory.'))
parser.add_argument('-v', dest='verbosity', action='count', default=0,
                    help='verbosity level')


parser.add_argument('--if-exists',
                    choices=["duplicate","overwrite","skip","doconly"],
                    default="skip",
                    help=("Specify the behavior when the destination file exists."
                          "\n  duplicate: Create a duplicate file in the same directory."
                          "\n  overwrite: Overwrite existing files and the metadata."
                          "\n  doconly:   Overwrite existing files but not the metadata."
                          "\n  skip:      Skip the file. (default)"))


parser.add_argument('--if-does-not-exist',
                    choices=["delete","skip"],
                    default="skip",
                    help=("Specify the behavior when the source file does not exist."
                          "\n  delete:    Remove the destination file."
                          "\n  skip:      Skip the file. (default)"))


parser.add_argument('-e', '--exclude', dest='exclude_patterns', action='append', default=[],
                    help='exclude a pattern from transfer (must be Python-regex)')

parser.add_argument('-r', '--remote-address', action='store', default='10.11.99.1', dest='host', metavar='<IP or hostname>',
                    help='remote address of the reMarkable')
parser.add_argument('--transfer-dir', metavar='<directory name>', dest='prepdir', type=str, default=default_prepdir,
                    help='custom directory to render files to-be-upload')
parser.add_argument('--debug', dest='debug', action='store_true', default=False,
                    help="Render documents, but don't copy to remarkable.")
parser.add_argument('-y', '--yes', dest='yes', action='store_true', default=False,
                    help="Do not ask for deletion.")

parser.add_argument('mode', type=str, choices=["push","pull","backup","+","-","clean"],
                    help=("Specifies the transfer mode."
                          "\n  push, +: push documents from this machine to the reMarkable"
                          "\n  pull, -: pull documents from the reMarkable to this machine"
                          "\n  backup:  pull all files from the remarkable to this machine (excludes still apply)"
                          "\n  clean:   performs a number of cleaning operations."
                          "\n           * clear the files in Trash"
                          "\n           * remove the orphaned files"
                          "\n           * remove empty directories"
                          "\n           * detect/select/remove duplicates"
                          "\n           We ask before performing each step unless -y|--yes."))

parser.add_argument('documents', metavar='documents', type=str, nargs='*',
                    help='Documents and folders to be pushed to the reMarkable')

args = parser.parse_args()

if args.mode == '+':
    args.mode = 'push'
elif args.mode == '-':
    args.mode = 'pull'

try:
    # verify host is a valid IP address string
    _ = ipaddress.ip_address(args.host)
    print(f"Assuming {args.host} is an ip address")
    args.host = "root@"+args.host
except ValueError as e:
    print(f"Assuming {args.host} is a host in SSH config")
    pass


ssh_command = " ".join(
    ["ssh",
     "-o PubkeyAcceptedKeyTypes=+ssh-rsa",
     "-o HostKeyAlgorithms=+ssh-rsa",
     "-o UserKnownHostsFile=/dev/null",
     "-o StrictHostKeyChecking=no",
     "-o ConnectTimeout=1",
     f"-S {ssh_socketfile}",])


def ssh(arg,dry=False,status=False):
    if args.verbosity >= 1:
        print(f'{ssh_command} {args.host} \'{arg}\'')
    if not dry:
        if status:
            return subprocess.getstatusoutput(f'{ssh_command} {args.host} \'{arg}\'')
        else:
            return subprocess.getoutput(f'{ssh_command} {args.host} \'{arg}\'')


class FileCollision(Exception):
    pass

class ShouldNeverHappenError(Exception):
    pass


#########################
#
#   Helper functions
#
#########################

def logmsg(lvl, msg):
    if lvl <= args.verbosity:
        print(msg)


def gen_did():
    """
    generates a uuid according to necessities (and marks it if desired for debugging and such)
    """
    did = str(uuid.uuid4())
    # did =  'f'*8 + did[8:]  # for debugging purposes
    return did


def construct_metadata(filetype, name, parent_id=''):
    """
    constructs a metadata-json for a specified document
    """
    meta={
        "visibleName": name,
        "parent": parent_id,
        "lastModified": str(int(time.time()*1000)),
        "metadatamodified": False,
        "modified": False,
        "pinned": False,
        "synced": False,
        "type": "CollectionType",
        "version": 0,
        "deleted": False,
    }

    if filetype in ['pdf', 'epub']:
        # changed from default
        meta["type"] = "DocumentType"

        # only for pdfs & epubs
        meta["lastOpened"] = meta["lastModified"]
        meta["lastOpenedPage"] = 0

    return meta


# from https://stackoverflow.com/questions/6886283/how-i-can-i-lazily-read-multiple-json-values-from-a-file-stream-in-python
def stream_read_json(f):
    start_pos = 0
    while True:
        try:
            obj = json.load(f)
            yield obj
            return
        except json.JSONDecodeError as e:
            f.seek(start_pos)
            json_str = f.read(e.pos)
            if json_str == '':
                return
            obj = json.loads(json_str)
            start_pos += e.pos
            yield obj


metadata_by_uuid = {}
metadata_by_name = {}
metadata_by_parent = {}
metadata_by_name_and_parent = {}

def retrieve_metadata():
    """
    retrieves all metadata from the device
    """
    print("retrieving metadata...")

    paths = ssh(f'ls -1 {xochitl_dir}/*.metadata').split("\n")
    with io.StringIO(ssh(f'cat {xochitl_dir}/*.metadata')) as f:
        for path, metadata in tqdm.tqdm(zip(paths, stream_read_json(f)), total=len(paths)):
            path = pathlib.Path(path)
            if ('deleted' in metadata and metadata['deleted']) or \
               ('parent' in metadata and metadata['parent'] == 'trash'):
                continue
            # metadata["uuid"] = path.stem
            uuid = path.stem
            metadata_by_uuid[uuid]                    = metadata

            if metadata["visibleName"] not in metadata_by_name:
                metadata_by_name[metadata["visibleName"]] = dict()
            metadata_by_name[metadata["visibleName"]][uuid] = metadata

            if metadata["parent"] not in metadata_by_parent:
                metadata_by_parent[metadata["parent"]] = dict()
            metadata_by_parent[metadata["parent"]][uuid] = metadata

            if (metadata["visibleName"], metadata["parent"]) in metadata_by_name_and_parent:
                raise FileCollision(f'Same file name "{metadata["visibleName"]}" under the same parent, not supported! Remove either file! {fullpath(metadata)}')
            metadata_by_name_and_parent[(metadata["visibleName"], metadata["parent"])] = (uuid, metadata)
    pass


def get_metadata_by_uuid(u):
    """
    retrieves metadata for a given document identified by its uuid
    """
    if u in metadata_by_uuid:
        return metadata_by_uuid[u]
    else:
        return None


def get_metadata_by_name(name):
    """
    retrieves metadata for all given documents that have the given name set as visibleName
    """
    if name in metadata_by_name:
        return metadata_by_name[name]
    else:
        return None


def get_metadata_by_parent(parent):
    """
    retrieves metadata for all given documents that have the given parent
    """
    if parent in metadata_by_parent:
        return metadata_by_parent[parent]
    else:
        return {}


def get_metadata_by_name_and_parent(name, parent):
    """
    retrieves metadata for all given documents that have the given parent
    """
    if (name, parent) in metadata_by_name_and_parent:
        return metadata_by_name_and_parent[(name, parent)]
    else:
        return None



def remove_uuid(u):
    """note --- not a complete implementation. does not remove from _name and _name_and_parent."""
    metadata = metadata_by_uuid[u]
    del metadata_by_uuid[u]
    if ("parent" in metadata) and (metadata["parent"] != ""):
        siblings = metadata_by_parent[metadata["parent"]]
        del siblings[u]
        if not metadata_by_parent[metadata["parent"]]:
            del metadata_by_parent[metadata["parent"]]


def curb_tree(node, excludelist):
    """
    removes nodes from a tree based on a list of exclude patterns;
    returns True if the root node is removed, None otherwise as the
    tree is curbed inplace
    """
    for exc in excludelist:
        if re.match(exc, node.get_full_path()) is not None:
            logmsg(2, "curbing "+node.get_full_path())
            return True

    uncurbed_children = []
    for ch in node.children:
        if not curb_tree(ch, excludelist):
            uncurbed_children.append(ch)

    node.children = uncurbed_children

    return False



def ask(msg):
    if args.yes:
        return True
    else:
        return input(msg+" [Enter,y,Y / n]") in ['', 'y', 'Y']


def fullpath(metadata):
    if ("parent" not in metadata) or (metadata["parent"] == ""):
        return "/" + metadata["visibleName"]
    else:
        parent_uuid = metadata["parent"]
        parent = metadata_by_uuid[parent_uuid]
        return fullpath(parent) + "/" + metadata["visibleName"]


#################################
#
#   Document tree abstraction
#
#################################


class Node:

    def __init__(self, name, parent=None):

        self.name = name
        self.parent = parent
        self.children = []

        self.gets_modified = False

        # now retrieve the document ID for this document if it already exists
        if parent is None:
            metadata = get_metadata_by_name_and_parent(self.name, "")
        else:
            metadata = get_metadata_by_name_and_parent(self.name, parent.id)

        if metadata:
            uuid, metadata = metadata
            self.id = uuid
            self.exists = True
        else:
            self.id = None
            self.exists = False


    def __repr__(self):
        return self.get_full_path()


    def add_child(self, node):
        """
        add a child to this Node and make sure it has a parent set
        """
        if node.parent is None:
            raise ShouldNeverHappenError("Child was added without having a parent set.")

        self.children.append(node)


    def get_full_path(self):
        if self.parent is None:
            return self.name
        else:
            return self.parent.get_full_path() + '/' + self.name


    def render_common(self, prepdir):
        """
        renders all files that are shared between the different DocumentTypes
        """

        logmsg(1, "preparing for upload: " + self.get_full_path())

        if self.id is None:
            self.id = gen_did()

        with open(f'{prepdir}/{self.id}.metadata', 'w') as f:
            if self.parent:
                metadata = construct_metadata(self.filetype, self.name, parent_id=self.parent.id)
            else:
                metadata = construct_metadata(self.filetype, self.name)
            json.dump(metadata, f, indent=4)

        with open(f'{prepdir}/{self.id}.content', 'w') as f:
            json.dump({}, f, indent=4)


    def render(self, prepdir):
        """
        This renders the given note, including DocumentType specifics;
        needs to be reimplemented by the subclasses
        """
        raise Exception("Rendering not implemented")


    def build(self):
        """
        This creates a document tree for all nodes that are direct and indirect
        descendants of this node.
        """
        raise Exception("Not implemented")


    def download(self, targetdir=pathlib.Path.cwd()):
        """
        retrieve document node from the remarkable to local system
        """
        raise Exception("Not implemented")


class Document(Node):

    def __init__(self, document, parent=None):

        self.doc = pathlib.Path(document)
        self.doctype = 'DocumentType'
        self.filetype = self.doc.suffix[1:] if self.doc.suffix.startswith('.') else self.doc.suffix

        super().__init__(self.doc.name, parent=parent)


    def render(self, prepdir):
        """
        renders an actual DocumentType tree node
        """
        if not self.exists:

            self.render_common(prepdir)

            os.makedirs(f'{prepdir}/{self.id}')
            os.makedirs(f'{prepdir}/{self.id}.thumbnails')
            shutil.copy(self.doc, f'{prepdir}/{self.id}.{self.filetype}')


    def build(self):
        """
        This creates a document tree for all nodes that are direct and indirect
        descendants of this node.
        """
        # documents don't have children, this one's easy
        return


    def download(self, targetdir=pathlib.Path.cwd()):
        """
        retrieve document node from the remarkable to local system
        """
        if args.dryrun:
            print("downloading document to", targetdir/self.name)
        else:

            logmsg(1, "retrieving " + self.get_full_path())
            os.chdir(targetdir)

            # documents we need to actually download
            filename = self.name if self.name.lower().endswith('.pdf') else f'{self.name}.pdf'
            if os.path.exists(filename):
                if if_exists == "skip":
                    logmsg(0, f"File {filename} already exists, skipping")
                elif if_exists == "overwrite":
                    try:
                        resp = urllib.request.urlopen(f'http://{args.host}/download/{self.id}/placeholder')
                        with open(filename, 'wb') as f:
                            f.write(resp.read())
                    except urllib.error.URLError as e:
                        print(f"{e.reason}: Is the web interface enabled? (Settings > Storage > USB web interface)")
                        sys.exit(2)
                else:
                    raise Exception("huh?")
        pass


class Folder(Node):

    def __init__(self, name, parent=None):
        self.doctype  = 'CollectionType'
        self.filetype = 'folder'
        super().__init__(name, parent=parent)


    def render(self, prepdir):
        """
        renders a folder tree node
        """
        if not self.exists:
            self.render_common(prepdir)

        for ch in self.children:
            ch.render(prepdir)


    def build(self):
        """
        This creates a document tree for all nodes that are direct and indirect
        descendants of this node.
        """
        for uuid, metadata in get_metadata_by_parent(self.id).items():
            if metadata['type'] == "CollectionType":
                ch = Folder(metadata['visibleName'], parent=self)
            else:
                name = metadata['visibleName']
                if not name.endswith('.pdf'):
                    name += '.pdf'
                ch = Document(name, parent=self)

            ch.id = uuid
            self.add_child(ch)
            ch.build()


    def download(self, targetdir=pathlib.Path.cwd()):
        """
        retrieve document node from the remarkable to local system
        """
        if args.dryrun:
            # folders we simply create ourselves
            print("creating directory", targetdir/self.name)
            for ch in self.children:
                ch.download(targetdir/self.name)
        else:

            logmsg(1, "retrieving " + self.get_full_path())
            os.chdir(targetdir)

            # folders we simply create ourselves
            os.makedirs(self.name, exist_ok=True)

            for ch in self.children:
                ch.download(targetdir/self.name)


def get_toplevel_files():
    """
    get a list of all documents in the toplevel My files drawer
    """
    toplevel_files = []
    for u, md in get_metadata_by_parent(""):
        toplevel_files.append(md['visibleName'])
    return toplevel_files



###############################
#
#   actual application logic
#
###############################


try:
    from termcolor import colored
except ImportError:
    colored = lambda s, c: s


size = shutil.get_terminal_size()
columns = size.columns
lines   = size.lines

# just print a filesystem tree for the remarkable representation of what we are going to create
def print_tree(node, padding):
    """
    prints a filesystem representation of the constructed document tree,
    including a note if the according node already exists on the remarkable or not
    """
    if node.gets_modified:
        note = " | !!! gets modified !!!"
        notelen = len(note)
        note = colored(note, 'red')
    elif node.exists:
        note = " | exists already"
        notelen = len(note)
        note = colored(note, 'green')
    else:
        note = " | upload"
        notelen = len(note)

    line = padding + node.name
    if len(line) > columns-notelen:
        line = line[:columns-notelen-3] + "..."
    line = line.ljust(columns-notelen)
    print(line+note)

    for ch in node.children:
        print_tree(ch, padding+"  ")


def construct_node_tree_from_disk(basepath, parent=None):
    """
    this recursively constructs the document tree based on the top-level
    document/folder data structure on disk that we put in initially
    """
    if args.verbosity >= 1:
        print(f"scanning {basepath}")
    path = pathlib.Path(basepath)
    if path.is_dir():
        node = Folder(path.name, parent=parent)
        for f in os.listdir(path):
            child = construct_node_tree_from_disk(path/f, parent=node)
            if child is not None:
                node.add_child(child)
        if not node.children:
            print(f"empty directory, ignored: {path}")
            return None
        else:
            return node

    elif path.is_file() and path.suffix.lower() in ['.pdf', '.epub']:
        node = Document(path, parent=parent)
        if node.exists:
            if args.if_exists == "skip":
                pass
            elif args.if_exists == "overwrite":
                # ok, we want to overwrite a document. We need to pretend it's not there so it gets rendered, so let's
                # lie to our parser here, claiming there is nothing
                node.exists = False
                node.gets_modified = True  # and make a note to properly mark it in case of a dry run

            elif args.if_exists == "doconly":
                # ok, we want to overwrite a document. We need to pretend it's not there so it gets rendered, so let's
                # lie to our parser here, claiming there is nothing
                node.exists = False
                node.gets_modified = True  # and make a note to properly mark it in case of a dry run
                # if we only want to overwrite the document file itself, but keep everything else,
                # we simply switch out the render function of this node to a simple document copy
                # might mess with xochitl's thumbnail-generation and other things, but overall seems to be fine
                node.render = lambda self, prepdir: shutil.copy(self.doc, f'{prepdir}/{self.id}.{self.filetype}')

            elif args.if_exists == "duplicate":
                # if we don't skip existing files, this file gets a new document ID
                # and becomes a new file next to the existing one
                node.id = gen_did()
                node.exists = False
            else:
                raise Exception("huh?")

        return node
    else:
        print(f"unsupported file type, ignored: {path}")
        return None


def push_to_remarkable():
    """
    push a list of documents to the reMarkable

    documents: list of documents
    destination: location on the device
    """

    if args.destination:
        # first, assemble the given output directory (-o) where everything shall be sorted into
        # into our document tree representation
        # then add the actual folders/documents to the tree at the anchor point
        folders = args.destination.split('/')
        root = anchor = Folder(folders[0])

        for folder in folders[1:]:
            ch = Folder(folder, parent=anchor)
            anchor.add_child(ch)
            anchor = ch
        for doc in args.documents:
            node = construct_node_tree_from_disk(doc, parent=anchor)
            if node is not None:
                anchor.add_child(node)

        # make it into a 1-element list to streamline code further down
        root = [root]

    else:
        root = []
        for doc in args.documents:
            node = construct_node_tree_from_disk(doc)
            if node is not None:
                root.append(node)

    # apply excludes
    curbed_roots = []
    for r in root:
        if not curb_tree(r, args.exclude_patterns):
            curbed_roots.append(r)

    root = curbed_roots

    # just print out the assembled document tree with appropriate actions

    for r in root:
        print_tree(r, "")
        print()

    try:
        if not args.dryrun:
            print(f"preparing the files to copy")
            for r in root:
                r.render(args.prepdir)

            if args.debug:
                print(f' --> Payload data can be found in {args.prepdir}')
                return

        command = f'rsync -a --info=progress2 -e "{ssh_command}" '
        if args.dryrun:
            command += " -n "
        if args.if_does_not_exist == "delete":
            command += " --delete "
        command += f' {args.prepdir}/ {args.host}:{xochitl_dir}/ ' # note: the last / is important
        print(f"running: {command}")
        subprocess.run(command, shell=True, check=True)

        if not args.dryrun:
            ssh(f'systemctl restart xochitl')

    finally:
        if args.prepdir == default_prepdir and not args.debug:  # aka we created it
            shutil.rmtree(args.prepdir)


def pull_from_remarkable():
    """
    pull documents from the remarkable to the local system

    documents: list of document paths on the remarkable to pull from
    """

    assert args.if_exists in ["skip", "overwrite"]

    if args.destination is None:
        destination_directory = pathlib.Path.cwd()
    else:
        destination_directory = pathlib.Path(args.destination).absolute()
    if not destination_directory.exists():
        print("Output directory non-existing, exiting.", file=sys.stderr)

    anchors = []
    for doc in args.documents:
        *parents, target = doc.split('/')
        local_anchor = None
        if parents:
            local_anchor = Folder(parents[0], parent=None)
            for par in parents[1:]:
                new_node = Folder(par, parent=local_anchor)
                local_anchor.add_child(new_node)
                local_anchor = new_node

        metadata = get_metadata_by_name(target)
        if metadata is not None:
            if metadata['type'] == 'DocumentType':
                new_node = Document(target, parent=local_anchor)
            else:
                new_node = Folder(target, parent=local_anchor)
            anchors.append(new_node)
        else:
            print(f"Cannot find {doc}, skipping")


    for a in anchors:
        a.build()
        if not curb_tree(a, args.exclude_patterns):
            a.download(targetdir=destination_directory)


def cleanup_deleted():
    print("removing trash files")

    deleted_uuids = []
    limit = 10
    for u, metadata in tqdm.tqdm(metadata_by_uuid.items()):
        if metadata['deleted']:
            deleted_uuids.append(u)

    if len(deleted_uuids) == 0:
        print('No deleted files found.')
        return False
    else:
        if ask(f'Clean up {len(deleted_uuids)} deleted files?'):
            ssh(f"rm -r {xochitl_dir}/{{{','.join(deleted_uuids)}}}*", dry=args.dryrun)
            return True
        else:
            return False


def cleanup_orphaned():
    print("removing files without metadata")
    files = ssh(f"ls -1 {xochitl_dir} | while read f ; do stem=${{f%%.*}} ; if ! [ -e {xochitl_dir}/$stem.metadata ] ; then echo $f ; fi ; done")
    l = len(files.split("\n"))
    if l == 1:
        print('No orphan files found.')
    elif args.dryrun:
        if args.verbosity >= 1:
            print(files)
    else:
        if args.verbosity >= 1:
            print(files)
        if ask(f'Clean up {l-1} orphaned files?'):
            ssh(f"ls -1 {xochitl_dir} | while read f ; do stem=${{f%%.*}} ; if ! [ -e {xochitl_dir}/$stem.metadata ] ; then rm {xochitl_dir}/$f ; fi ; done")


def cleanup_duplicates():
    """detect, select, remove duplicates. If there are notes, merge them."""

    print("computing md5sum of each file on the reMarkable device... it takes some time in the first run.")
    results = ssh((f"for f in {xochitl_dir}/*.pdf ; do "
                   "if [ ! -e $f.md5sum ] ; then "
                   "md5sum $f > $f.md5sum ;"
                   "fi ;"
                   "done ;"
                   f"cat {xochitl_dir}/*.md5sum")).split("\n")
    database = dict()
    duplicates = set()
    for line in results:
        try:
            md5, filename = line.split()
        except Exception as e:
            print(line)
            raise e
        u = os.path.basename(os.path.splitext(filename)[0])
        if md5 not in database:
            database[md5] = []
        database[md5].append(u)
        if len(database[md5])>=2:
            duplicates.add(md5)


    deleted_uuids = []
    for j, md5 in enumerate(duplicates):
        print(f"({j:3d}/{len(duplicates)}) found {len(database[md5])} duplicates for md5sum {md5}:")
        try:
            tmp = []

            for u in database[md5]:
                metadata = get_metadata_by_uuid(u)
                if metadata is None:
                    print(f"weird, the metadata for {u} does not exist, skipping (in: {database[md5]})")
                    continue
                lastmodified = metadata["lastModified"]
                try:
                    lastmodified = int(lastmodified)
                except ValueError as e:
                    print(f"error while reading the last modified date of file {fullpath(metadata)}")
                    raise e
                tmp.append((lastmodified, u, metadata))

            tmp = sorted(tmp,reverse=True)

            for i, (lastmodified, u, metadata) in enumerate(tmp):
                lastmodified = datetime.datetime.fromtimestamp(lastmodified//1000)
                if i == 0:
                    prefix="(* newest)"
                else:
                    prefix="          "
                print(f"{prefix} {i}, uuid {u}, modified {lastmodified}, {fullpath(metadata)}")

        except KeyError:
            print("this should not happen...")
            continue

        while True:
            if len(tmp) == 1:
                print("Due to the anomaly, there is only one candidate which I keep.")
                i = 0
                break
            s = input("Hit ENTER for default(*), select a number, or hit n to skip this file, or hit N to stop: ")
            if s == "":
                i = 0
                break
            elif s == "n":
                i = -1
                break
            elif s == "N":
                i = -2
                break
            else:
                try:
                    i = int(s)
                except ValueError:
                    print(f"Input parsing error. ('{s}') Try again")
                    continue
                if 0 <= i < len(tmp):
                    break
                else:
                    print(f"enter a number from 0 to {len(tmp)-1}.")
                    continue

        if i == -1:
            print(f"skipping this file.")
            continue
        if i == -2:
            print(f"cleanup stopped.")
            break

        _, keep, _ = tmp[i]

        for _, u, _ in tmp:
            if u == keep:
                continue
            remove_uuid(u)
            ssh(f"rm -rv {xochitl_dir}/{u}*", dry=args.dryrun)
            deleted_uuids.append(u)
        print(f'Removed {len(tmp)-1} duplicates.')


    print(f'Removed {len(deleted_uuids)} duplicates in total.')
    return len(deleted_uuids) > 0


def cleanup_emptydir():
    """remove empty directory"""

    deleted_uuids = []
    empty_found = True
    iteration = 1
    while empty_found:
        print(f"iteration {iteration}")
        iteration += 1
        empty_found = False
        _deleted_uuids = []
        for u, metadata in metadata_by_uuid.items():
            if metadata['type'] != "CollectionType":
                continue
            if u not in metadata_by_parent:
                print(f"empty: {fullpath(metadata)}")
                _deleted_uuids.append(u)
                empty_found = True
        # do not remove entries within a loop !
        for u in _deleted_uuids:
            remove_uuid(u)
        deleted_uuids.extend(_deleted_uuids)

    if len(deleted_uuids) == 0:
        print('No empty directories found.')
        return False
    else:
        if ask(f'Clean up {len(deleted_uuids)} empty directories?'):
            ssh(f"rm -r {xochitl_dir}/{{{','.join(deleted_uuids)}}}*", dry=args.dryrun)
            return True
        else:
            return False


ssh_connection = None
try:

    ssh_connection = subprocess.Popen(f'{ssh_command} {args.host} -M -N -q ', shell=True)

    # quickly check if we actually have a functional ssh connection (might not be the case right after an update)
    status, checkmsg = ssh("/bin/true",status=True)
    if status != 0:
        print("ssh connection does not work, verify that you can manually ssh into your reMarkable. ssh itself commented the situation with:")
        print("msg:",checkmsg)
        sys.exit(255)

    retrieve_metadata()
    if args.mode == 'push':
        push_to_remarkable()
    elif args.mode == 'pull':
        pull_from_remarkable()
    elif args.mode == 'backup':
        args.documents = get_toplevel_files()
        pull_from_remarkable()
    elif args.mode == 'clean':
        r1 = cleanup_deleted()
        cleanup_orphaned()
        r2 = cleanup_duplicates()
        r3 = cleanup_emptydir()
        if not args.dryrun and (r1 or r2 or r3):
            ssh(f'systemctl restart xochitl')

finally:
    if ssh_connection is not None:
        print("terminating ssh connection")
        ssh_connection.terminate()

