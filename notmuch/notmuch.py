"""
    (c) 2010 Sebastian Spaeth Sebastian@SSpaeth.de
    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import subprocess, re, logging, os, time, email.utils
from cnotmuch.notmuch import Database, Query
from cnotmuch.message import Messages, Message

#---------------------------------------------------------------------------
class SyncMessage(Message):
#---------------------------------------------------------------------------
    """
    Represents a message as returned by notmuch.

    * Valid instance variables self.*

       The following vars are set on the initial parsing 
       (or dynamically created as properties):
       .id (msg id, is set on the initial parsing)
       .file (full file name including path, is set on the initial parsing)
       .maildirflags: a set() of MailDir flags
       .tags: a set() of notmuch tags
       and many more properties. Please refer to the class documentation
       for details.

      If the following two variables contain a set() of tags/flags that are 
      different from .tags .notmuchtags, a sync_msg_tags will write those 
      changes:
      .sync_maildirflags: a set() of MailDir flags to be used
      .sync_tags: a set() of notmuch tags to be used

      .is_valid: is True if parsing found a message, False otherwise
    """

    def __init__(self, msg):
       """inititialize a message from a Message() object"

       """
       #Init this SyncMessage instance with the pointer to the notmuch_message_t
       super(SyncMessage, self).__init__(msg._msg, msg._parent)
       self.filename = self.get_filename()
       flags = re.sub('^.*:[12],([A-Z]*)$','\\1',self.filename)
       self.maildirflags = set(flags)
       self.sync_maildirflags = None
       self.tags = set(self.get_tags())
       self.sync_tags = None

    def __del__(self):
        #override __del__ to essentially do nothing. We don't want to free
        #self._msg in this inherited instance, as it is freed in the Message()
        #this is inherited from. The Message() will otherwise crash, it seems.
        pass

    def sync_msg_tags(self, dryrun=False):
        """ Sync up changed maildir tags and/or notmuch tags.
        It examines self.tags|sync_tags and self.maildirflags|sync_maildirflags
        sync_* contains the new set of tags to be applied
        """

        if (self.sync_maildirflags is not None and self.maildirflags ^ self.sync_maildirflags):
            #sync_tags differs from tags. Need to sync up maildir tags
            newtags = "".join(sorted(self.sync_maildirflags))
            newfile = re.sub(r'^(.*:[12],)([A-Z]*)$',"\\1"+newtags,self.filename)
            logging.debug("Maildir flags for %s (%s -> '%s')" % 
                          (self.get_message_id(),
                           sorted(self.maildirflags),
                           newtags))
            #check if we need to move from 'new' to 'cur' dir
            if 'S' in self.sync_maildirflags:
                # split BASEDIR / NEW / FILENAME into components
                (path, filename) = os.path.split(newfile)
                (basedir, curdir)= os.path.split(path)
                curdir = re.sub("^new$","cur", curdir)
                newfile = os.path.join(basedir, curdir, filename)

            if not dryrun:
                try:
                    os.rename(self.filename, newfile)
                except OSError, e:
                    if e.errno == 2:
                        logging.info("Renaming not possible, file %s not found"
                                     % (self.filename))
                    else:
                        raise OSError(e)

        if (self.sync_tags is not None
            and self.tags ^ self.sync_tags):
            #sync_notmuchtags differs. Need to sync notmuch tags
            logging.debug("Sync tags: +%s -%s for id:%s" %
                          (list(self.sync_tags-self.tags),
                           list(self.tags-self.sync_tags),
                           self.get_message_id()))

            if not dryrun:
                #actually modify the notmuch tag database
                self.freeze()
                self.remove_all_tags()
                for tag in self.sync_tags:
                    self.add_tag(tag)
                self.thaw()
                #TODO: catch error:
                #logging.error("Notmuch failed: %s" % (stderr))

#---------------------------------------------------------------------------
class Notmuch:
#---------------------------------------------------------------------------
    """
    python abstraction to the notmuch command line interface. 

    Notmuch represents a specific request. Calling its method will cause the actual notmuch calls.
    It uses the logging module for logging, so you can set that up to log 
    to files etc.

    :param logger: A logging.Logger to be used for logging
    :type logger: logging.Logger
    :rtype: the initialized Notmuch instance
    """

    def __init__(self, logger=None):
        """Initialize the notmuch object"""
        if logger:
            self.logger = logger
        else:
            self.logger = logging.getLogger()

        #open the database as read-only
        self.db_ro = Database()

    def prune(self, crit="tag:delete or tag:maildir::trashed", dryrun=False):
        """ Physically delete all mail files matching 'tag'. 
        Returns the number of matched mails.
        If dryrun == True, it will not actually delete them.
        """
        msgs = Query(self.db_ro,crit).search_messages()
        to_delete = 0

        if msgs == None:
            #TODO, catch notmuch.show exceptions
            logging.error("Could not prune messages due to notmuch error.")
            return None

        if dryrun:
            to_delete = len(msgs)
            self.logger.info("Would have deleted %d messages." %
                             (to_delete))
            return to_delete

        deleted = 0
        for msg in msgs:
            to_delete += 1
            try:
                os.unlink(msg.get_filename())
                deleted += 1
            except OSError, e:
                #skip errors
                pass

        self.logger.info("Deleted %d of %d messages." %
                             (deleted, to_delete))
        return to_delete

    def syncTags(self,frommaildir=False,dryrun=False, all_mails=None):
        """ sync the unread Tags. It does not really go through all mail files,
        but compares the stored file name with the notmuch tags.
        It will take the maildir tags as authoritative if 'frommaildir' or 
        the notmuch tags otherwise. 
        
        Flags handled:
        * "S": the user has viewed this message. Corresponds to "unread" tag
        * "T" (deleted): the user has moved this message to the trash.
        * "D" (draft):
        * "F" (flagged): user-defined flag; toggled at user discretion. 
        Not handled:
        * Flag "P" (passed): the user has resent/forwarded/bounced this message.
        * Flag "R" (replied): the user has replied to this message.
        """
        """
        #This is the version that uses the dateparser branch. Comment out
        #until it will work.
        if not all_mails:
            #search for messages from beginning of last month until 2036
            # (we have a  year 2036 problem)
            searchterm = "date:lastmonth..2036"
        else:
            #search for all messages between year 0 and 2036 
            # (we have a  year 2036 problem)
            searchterm = "date:1970..2036"
        """
        now = int(time.time())        
        if not all_mails:
            #search for all messages dating 30 days back and forth in time
            searchterm = "%d..%d" % (now-2592000,now+2592000)
        else:
            searchterm = "0..%d" % (now+2592000)

        #we need a rw database if we might need to modify notmuch tags
        if frommaildir and not dryrun:
            db = Database(mode=Database.MODE.READ_WRITE)
        else:
            db = self._db

        #fetch all messages
        msgs = Query(db,searchterm).search_messages()

        if msgs == None:
            logging.error("Could not sync messages due to notmuch error.")
            return None

        tag_trans={'delete':'T','draft':'D','flagged':'F'}
        tag_trans_inverse = dict((tag_trans[x], x) for x in tag_trans) # a bit clumsy ?!
        # check all messages for inconsistencies
        num_modified = 0
        total_msgs   = 0
        for msg in msgs:
            #create a derived SyncMessage instance from our Message()
            m = SyncMessage(msg)
            total_msgs += 1
            modified = False
            # handle SEEN vs unread tags:
            if not (('S' in m.maildirflags) ^ ('unread' in m.tags)):
                modified = True
                if frommaildir:
                    # Flip the unread notmuch tag
                    m.sync_tags = m.tags ^ set(['unread'])
                else:
                    # Flip the SEEN maildir tag
                    m.sync_maildirflags = m.maildirflags ^ set(['S'])

            #handle all other tag consistencies
            #these MailDir flags in tag_trans are wrong
            wrongflags = (set([tag_trans.get(x) for x in m.tags]) \
                          ^ set(tag_trans.values()) & m.maildirflags ) \
                          - set([None])     # finally remove None from result
            if wrongflags:
                modified = True
                if frommaildir:
                    # Flip the maildir flags
                    if m.sync_tags == None:
                        m.sync_tags = set()
                    m.sync_tags = m.sync_tags | m.tags ^ \
                        set ([tag_trans_inverse.get(x) for x in wrongflags])
                    #logging.debug("Flip nm %s to %s (flags %s)" % 
                    #            (m.tags,m.sync_tags, wrongflags))
                else:
                    # Flip the maildir flags
                    if m.sync_maildirflags == None:
                        m.sync_maildirflags = set()
                    m.sync_maildirflags = m.sync_maildirflags | (m.maildirflags ^ wrongflags)
                    #logging.debug("Flip f %s to %s %s (notmuch %s)" % 
                    #           (m.maildirflags,m.sync_maildirflags, wrongflags, m.tags))

            if modified:
                num_modified += 1 
                m.sync_msg_tags(dryrun=dryrun)

            #delete our temporary SyncMessage
            del(m)

        logging.info("Synced %d messages. %d modified."
                     % (total_msgs, num_modified))
