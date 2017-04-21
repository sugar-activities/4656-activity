# Copyright 2013 Agustin Zubiaga <aguz@sugarlabs.org>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import logging
from gettext import gettext as _

from gi.repository import GObject
GObject.threads_init()
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import WebKit

import telepathy
import dbus
import os.path
import json
import socket

from sugar3.activity import activity
from sugar3.activity.widgets import ActivityToolbarButton
from sugar3.activity.widgets import StopButton
from sugar3.graphics.toolbarbox import ToolbarBox
from sugar3.graphics.toolbutton import ToolButton
from sugar3.datastore import datastore
from sugar3.graphics.xocolor import XoColor
from sugar3 import profile
from sugar3.graphics.objectchooser import ObjectChooser

import downloadmanager
from filepicker import FilePicker
import server
import utils

JOURNAL_STREAM_SERVICE = 'journal-activity-http'

# directory exists if powerd is running.  create a file here,
# named after our pid, to inhibit suspend.
POWERD_INHIBIT_DIR = '/var/run/powerd-inhibit-suspend'


class JournalShare(activity.Activity):

    def __init__(self, handle):

        self._fileserver_tube_id = -1
        activity.Activity.__init__(self, handle)

        # a list with the object_id of the shared items.
        # if is a only element == '*' means all the favorite items
        # are selected
        self._activity_path = activity.get_bundle_path()
        self._activity_root = activity.get_activity_root()
        self._jm = JournalManager(self._activity_root)

        # master is the activity in the activity who started the communication
        self._master = False
        self.ip = '0.0.0.0'

        if not self.shared_activity:
            # Get a free socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            sock.bind(('', 0))
            sock.listen(socket.SOMAXCONN)
            _ipaddr, self.port = sock.getsockname()
            sock.shutdown(socket.SHUT_RDWR)
            logging.error('Using port %d', self.port)

            #TODO: check available port
            server.run_server(self._activity_path, self._activity_root,
                              self._jm, self.port)
            self._master = True

        toolbar_box = ToolbarBox()

        activity_button = ActivityToolbarButton(self)
        toolbar_box.toolbar.insert(activity_button, 0)
        activity_button.show()

        add_button = ToolButton('list-add')
        add_button.set_tooltip(_('Add item to share'))
        add_button.show()
        add_button.connect('clicked', self.__add_clicked_cb)
        toolbar_box.toolbar.insert(add_button, -1)

        if self._master:
            add_favorites_button = ToolButton('emblem-favorite')
            add_favorites_button.set_tooltip(_('Add favorite items to share'))
            add_favorites_button.show()
            add_favorites_button.connect('clicked',
                                         self.__add_favorites_clicked_cb)
            toolbar_box.toolbar.insert(add_favorites_button, -1)

        separator = Gtk.SeparatorToolItem()
        separator.props.draw = False
        separator.set_expand(True)
        separator.show()
        toolbar_box.toolbar.insert(separator, -1)

        stopbutton = StopButton(self)
        toolbar_box.toolbar.insert(stopbutton, -1)
        stopbutton.show()

        self.set_toolbar_box(toolbar_box)
        toolbar_box.show()

        self.view = WebKit.WebView()
        self.view.connect('mime-type-policy-decision-requested',
                          self.__mime_type_policy_cb)
        self.view.connect('download-requested', self.__download_requested_cb)

        try:
            self.view.connect('run-file-chooser', self.__run_file_chooser)
        except TypeError:
            # Only present in WebKit1 > 1.9.3 and WebKit2
            pass

        self.view.load_html_string('<html><body>Loading...</body></html>',
                                   'file:///')

        self.view.show()
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(self.view)
        scrolled.show()
        self.set_canvas(scrolled)

        # collaboration
        self.unused_download_tubes = set()
        self.connect("shared", self._shared_cb)

        if self.shared_activity:
            # We're joining
            if self.get_shared():
                # Already joined for some reason, just connect
                self._joined_cb(self)
            else:
                # Wait for a successful join before trying to connect
                self.connect("joined", self._joined_cb)
        else:
            self.view.load_uri('http://0.0.0.0:%d/web/index.html' %
                               self.port)
            # if I am the server
            self._inhibit_suspend()

    def _joined_cb(self, also_self):
        """Callback for when a shared activity is joined.
        Get the shared tube from another participant.
        """
        self.watch_for_tubes()
        GObject.idle_add(self._get_view_information)

    def __add_clicked_cb(self, button):
        chooser = ObjectChooser(self)
        try:
            result = chooser.run()
            if result == Gtk.ResponseType.ACCEPT:
                logging.debug('ObjectChooser: %r',
                              chooser.get_selected_object())
                jobject = chooser.get_selected_object()
                # add the information about the sharer
                user_data = utils.get_user_data()
                jobject.metadata['shared_by'] = json.dumps(user_data)
                # And add a comment to the Journal entry
                if 'comments' in jobject.metadata:
                    comments = json.loads(jobject.metadata['comments'])
                else:
                    comments = []
                comments.append(
                    {'from': user_data['from'],
                     'message': _('I shared this.'),
                     'icon-color': '[%s,%s]' %
                        (user_data['icon'][0], user_data['icon'][1])})
                jobject.metadata['comments'] = json.dumps(comments)

                if jobject and jobject.file_path:
                    if self._master:
                        datastore.write(jobject)
                        self._jm.append_to_shared_items(jobject.object_id)
                    else:
                        tmp_path = os.path.join(self._activity_root,
                                                'instance')
                        logging.error('temp_path %s', tmp_path)
                        packaged_file_path = utils.package_ds_object(
                            jobject, tmp_path)
                        url = 'ws://%s:%d/websocket/upload' % (self.ip,
                                                               self.port)
                        uploader = utils.Uploader(packaged_file_path, url)
                        uploader.connect('uploaded', self.__uploaded_cb)
                        cursor = Gdk.Cursor.new(Gdk.CursorType.WATCH)
                        self.get_window().set_cursor(cursor)
                        uploader.start()
        finally:
            chooser.destroy()
            del chooser

    def __uploaded_cb(self, uploader):
        self.get_window().set_cursor(None)

    def __add_favorites_clicked_cb(self, button):
        self._jm.set_shared_items(['*'])

    def _get_view_information(self):
        # Pick an arbitrary tube we can try to connect to the server
        try:
            tube_id = self.unused_download_tubes.pop()
        except (ValueError, KeyError), e:
            logging.error('No tubes to connect from right now: %s',
                          e)
            return False

        GObject.idle_add(self._set_view_url, tube_id)
        return False

    def _set_view_url(self, tube_id):
        chan = self.shared_activity.telepathy_tubes_chan
        iface = chan[telepathy.CHANNEL_TYPE_TUBES]
        addr = iface.AcceptStreamTube(
            tube_id,
            telepathy.SOCKET_ADDRESS_TYPE_IPV4,
            telepathy.SOCKET_ACCESS_CONTROL_LOCALHOST, 0,
            utf8_strings=True)
        logging.error('Accepted stream tube: listening address is %r', addr)
        # SOCKET_ADDRESS_TYPE_IPV4 is defined to have addresses of type '(sq)'
        assert isinstance(addr, dbus.Struct)
        assert len(addr) == 2
        assert isinstance(addr[0], str)
        assert isinstance(addr[1], (int, long))
        assert addr[1] > 0 and addr[1] < 65536
        self.ip = addr[0]
        self.port = int(addr[1])

        self.view.load_uri('http://%s:%d/web/index.html' %
                           (self.ip, self.port))
        return False

    def _start_sharing(self):
        """Share the web server."""

        # Make a tube for the web server
        chan = self.shared_activity.telepathy_tubes_chan
        iface = chan[telepathy.CHANNEL_TYPE_TUBES]
        self._fileserver_tube_id = iface.OfferStreamTube(
            JOURNAL_STREAM_SERVICE, {},
            telepathy.SOCKET_ADDRESS_TYPE_IPV4,
            ('127.0.0.1', dbus.UInt16(self.port)),
            telepathy.SOCKET_ACCESS_CONTROL_LOCALHOST, 0)

    def watch_for_tubes(self):
        """Watch for new tubes."""
        if self._master:
            # I am sharing, then, don't try to connect to the tubes
            return

        tubes_chan = self.shared_activity.telepathy_tubes_chan

        tubes_chan[telepathy.CHANNEL_TYPE_TUBES].connect_to_signal(
            'NewTube', self._new_tube_cb)
        tubes_chan[telepathy.CHANNEL_TYPE_TUBES].ListTubes(
            reply_handler=self._list_tubes_reply_cb,
            error_handler=self._list_tubes_error_cb)

    def _new_tube_cb(self, tube_id, initiator, tube_type, service, params,
                     state):
        """Callback when a new tube becomes available."""
        logging.error('New tube: ID=%d initator=%d type=%d service=%s '
                      'params=%r state=%d', tube_id, initiator, tube_type,
                      service, params, state)
        if self._fileserver_tube_id == tube_id:
            logging.error('This is my tube!... Quit')
            return

        if service == JOURNAL_STREAM_SERVICE:
            logging.error('I could download from that tube')
            self.unused_download_tubes.add(tube_id)
            GObject.idle_add(self._get_view_information)

    def _list_tubes_reply_cb(self, tubes):
        """Callback when new tubes are available."""
        for tube_info in tubes:
            self._new_tube_cb(*tube_info)

    def _list_tubes_error_cb(self, e):
        """Handle ListTubes error by logging."""
        logging.error('ListTubes() failed: %s', e)

    def _shared_cb(self, activityid):
        """Callback when activity shared.
        Set up to share the document.
        """
        # We initiated this activity and have now shared it, so by
        # definition the server is local.
        logging.error('Activity became shared')
        self.watch_for_tubes()
        self._start_sharing()

    def __mime_type_policy_cb(self, webview, frame, request, mimetype,
                              policy_decision):
        if not self.view.can_show_mime_type(mimetype):
            policy_decision.download()
            return True

        return False

    def __run_file_chooser(self, browser, request):
        picker = FilePicker(self)
        chosen = picker.run()
        picker.destroy()
        if chosen:
            logging.error('CHOSEN %s', chosen)
            request.select_files([chosen])
        elif hasattr(request, 'cancel'):
            # WebKit2 only
            request.cancel()
        return True

    def __download_requested_cb(self, browser, download):
        downloadmanager.add_download(download, browser)
        return True

    def read_file(self, file_path):
        f = open(file_path)
        json_data = f.read()
        f.close()
        # the information is saved in a dictionary
        # now is only the list of shared items
        # but later we can add more info
        state = json.loads(json_data)
        if 'shared_items' in state:
            self._jm.set_shared_items(state['shared_items'])

    def write_file(self, file_path):
        state = {}
        state['shared_items'] = self._jm.get_shared_items()
        f = open(file_path, 'w')
        f.write(json.dumps(state))
        f.close()

    def can_close(self):
        self._allow_suspend()
        # remove temporary files
        instance_path = self._activity_root + '/instance/'
        for file_name in os.listdir(instance_path):
            file_path = os.path.join(instance_path, file_name)
            if os.path.isfile(file_path):
                os.remove(file_path)

        return True

    # power management (almost copied from clock activity)

    def powerd_running(self):
        return os.access(POWERD_INHIBIT_DIR, os.W_OK)

    def _inhibit_suspend(self):
        if self.powerd_running():
            fd = open(POWERD_INHIBIT_DIR + "/%u" % os.getpid(), 'w')
            fd.close()
            return True
        else:
            return False

    def _allow_suspend(self):
        if self.powerd_running():
            if os.path.exists(POWERD_INHIBIT_DIR + "/%u" % os.getpid()):
                os.unlink(POWERD_INHIBIT_DIR + "/%u" % os.getpid())
            return True
        else:
            return False


class JournalManager(GObject.GObject):

    __gsignals__ = {'updated': (GObject.SignalFlags.RUN_FIRST, None, ([]))}

    def __init__(self, activity_root):
        GObject.GObject.__init__(self)
        self._instance_path = activity_root + '/instance/'
        self._shared_items = []
        try:
            self.nick_name = profile.get_nick_name()
        except:
            logging.exception('Can''t get nick_name')
            self.nick_name = ''
        try:
            self.xo_color = profile.get_color()
        except:
            logging.exception('Can''t get xo_color')
            self.xo_color = XoColor()

        # write json files
        owner_info_file_path = self._instance_path + 'owner_info.json'
        owner_info_file = open(owner_info_file_path, 'w')
        owner_info_file.write(self.get_journal_owner_info())
        owner_info_file.close()

        self._update_temporary_files()

    def set_shared_items(self, shared_items):
        self._shared_items = shared_items
        self._update_temporary_files()

    def _update_temporary_files(self):
        selected_file_path = os.path.join(self._instance_path,
                                          'selected.json')
        selected_file = open(selected_file_path, 'w')
        selected_file.write(self._prepare_shared_items())
        selected_file.close()
        self.emit('updated')

    def get_shared_items(self):
        return self._shared_items

    def append_to_shared_items(self, item):
        self._shared_items.append(item)
        self._update_temporary_files()

    def get_journal_owner_info(self):
        info = {}
        info['nick_name'] = self.nick_name
        info['stroke_color'] = self.xo_color.get_stroke_color()
        info['fill_color'] = self.xo_color.get_fill_color()
        logging.error('INFO %s', info)
        return json.dumps(info)

    def add_downloader(self, object_id, name, icon):
        """
        Add to the metadata downloaded_by field, the information
        about who downloaded one object
        """
        dsobj = datastore.get(object_id)
        downloaded_by = []
        if 'downloaded_by' in dsobj.metadata:
            downloaded_by = json.loads(dsobj.metadata['downloaded_by'])
        # add the user data
        user_data = {}
        user_data['from'] = name
        user_data['icon'] = icon
        downloaded_by.append(user_data)
        dsobj.metadata['downloaded_by'] = json.dumps(downloaded_by)
        datastore.write(dsobj)
        self._update_temporary_files()

    def create_object(self, file_path, metadata, preview_content):
        new_dsobject = datastore.create()
        #Set the file_path in the datastore.
        new_dsobject.set_file_path(file_path)

        for key in metadata.keys():
            new_dsobject.metadata[key] = metadata[key]

        if preview_content is not None and preview_content != '':
            new_dsobject.metadata['preview'] = \
                dbus.ByteArray(preview_content)
        datastore.write(new_dsobject)
        if self._shared_items == ['*']:
            # mark as favorite
            new_dsobject.metadata['keep'] = '1'
            self._update_temporary_files()
        else:
            self.append_to_shared_items(new_dsobject.object_id)
        return False

    def _prepare_shared_items(self):
        results = []
        if not self._shared_items:
            return json.dumps(results)

        if self._shared_items == ['*']:
            dsobjects, _nobjects = datastore.find({'keep': '1'})
        else:
            dsobjects = []
            for object_id in self._shared_items:
                dsobjects.append(datastore.get(object_id))

        for dsobj in dsobjects:
            title = ''
            desc = ''
            comment = []
            shared_by = {}
            downloaded_by = []
            object_id = dsobj.object_id
            if hasattr(dsobj, 'metadata'):
                if 'title' in dsobj.metadata:
                    title = dsobj.metadata['title']
                if 'description' in dsobj.metadata:
                    desc = dsobj.metadata['description']
                if 'comments' in dsobj.metadata:
                    try:
                        comment = json.loads(dsobj.metadata['comments'])
                    except:
                        comment = []
                if 'shared_by' in dsobj.metadata:
                    shared_by = json.loads(dsobj.metadata['shared_by'])
                if 'downloaded_by' in dsobj.metadata:
                    downloaded_by = json.loads(
                        dsobj.metadata['downloaded_by'])
            else:
                logging.debug('dsobj has no metadata')

            utils.package_ds_object(dsobj, self._instance_path)

            results.append({'title': str(title), 'desc': str(desc),
                            'comment': comment, 'id': str(object_id),
                            'shared_by': shared_by,
                            'downloaded_by': downloaded_by})
        logging.error(results)
        return json.dumps(results)
