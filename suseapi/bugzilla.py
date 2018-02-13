# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2015 Michal Čihař <mcihar@suse.cz>
#
# This file is part of python-suseapi
# <https://github.com/openSUSE/python-suseapi>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
'''
Generic access to Novell Bugzilla.

It uses XML to load the data (when applicable) and HTML forms to update it.
'''

# pylint: disable=import-error
from six.moves.urllib.parse import urljoin
# pylint: disable=import-error
from lxml import etree as ElementTree
import dateutil.parser
import traceback
import re
import logging
from bs4 import BeautifulSoup
from weblib.error import DataNotFound

from suseapi.browser import WebScraper, WebScraperError, webscraper_safely
from .compat import text_type


SR_MATCH = re.compile(r'\[(\d+)\]')

IGNORABLE_FIELDS = frozenset((
    'commentprivacy',
    'comment_is_private',
    'addselfcc',
    'groups',
))


class BugzillaError(WebScraperError):
    '''Generic error'''

    def __init__(self, error, bug_id=None):
        super(BugzillaError, self).__init__(error)
        self.bug_id = bug_id
        self.error = error

    def __str__(self):
        if self.bug_id is not None:
            return "%s: %s: %s" % (self.__doc__, self.error, self.bug_id)
        return "%s: %s" % (self.__doc__, self.error)


class BugzillaNotPermitted(BugzillaError):
    '''Access not permitted'''
    pass


class BugzillaNotFound(BugzillaError):
    '''Bug was not found'''
    pass


class BugzillaInvalidBugId(BugzillaError):
    '''Bug Id is invalid'''
    pass


class BugzillaConnectionError(BugzillaError):
    '''Connection related error'''
    pass


class BugzillaLoginFailed(BugzillaConnectionError):
    '''Login has failed'''
    pass


class BuglistTooLarge(BugzillaError):
    '''Search returned too many entries'''
    pass


class BugzillaUpdateError(BugzillaConnectionError):
    '''Error while updating bug'''
    pass


def escape_xml_text(data):
    '''
    Fix some XML errors in bugzilla xml, which confuse proper XML parser.
    '''
    replacement_map = dict([
        (chr(orig), '\\x%02d' % orig) for orig in range(32)
        # skipt newline, carriage return and tabulator chars
        if orig not in (9, 10, 13)
    ])

    substrs = sorted(replacement_map, key=len, reverse=True)
    regexp = re.compile('|'.join([re.escape(s) for s in substrs]))
    return regexp.sub(lambda match: replacement_map[match.group(0)], data)


class Bug(object):
    '''
    Class holding bug information.
    '''

    def __init__(self, bug_et, anonymous=False):
        error = bug_et.get('error')
        self.bug_id = None
        if error is not None:
            bug_id = bug_et.find("bug_id")
            if bug_id is not None:
                self.bug_id = bug_id.text
            if error == 'NotPermitted':
                raise BugzillaNotPermitted(error, self.bug_id)
            if error == 'NotFound':
                raise BugzillaNotFound(error, self.bug_id)
            if error == 'InvalidBugId':
                raise BugzillaInvalidBugId(error, self.bug_id)
            raise BugzillaError(error)
        self.cc_list = []
        self.groups = []
        self.comments = []
        self.attachments = []
        self.aliases = []
        self.delta_ts = None
        self.creation_ts = None
        self.anonymous = anonymous
        self.flags = []
        for element in bug_et.getchildren():
            self.process_element(element)

    def has_nonempty(self, name):
        '''
        Checks whether object has nonempty attribute.
        '''
        value = getattr(self, name, None)
        return value is not None and value != ''

    def process_element(self, element):
        '''
        Parses data from element tree instance and stores them within
        this object.
        '''
        if element.tag == 'cc':
            self.cc_list.append(element.text)
        elif element.tag == 'alias':
            self.aliases.append(element.text)
        elif element.tag == 'group':
            self.groups.append(element.text)
        elif element.tag == 'creation_ts':
            self.creation_ts = dateutil.parser.parse(element.text)
        elif element.tag == 'delta_ts':
            self.delta_ts = dateutil.parser.parse(element.text)
        elif element.tag == 'flag':
            self.process_flag(element)
        elif not element.getchildren():
            setattr(self, element.tag, element.text)
        elif element.tag == 'long_desc':
            self.process_comment(element)
        elif element.tag == 'attachment':
            self.process_attachment(element)

    def process_attachment(self, element):
        '''
        Stores attachment data within this object.
        '''
        self.attachments.append({
            'attachid': element.find('attachid').text,
            'desc': element.find('desc').text,
            'date': dateutil.parser.parse(element.find('date').text),
            'filename': element.find('filename').text,
            'type': element.find('type').text,
            'size': element.find('size').text,
            'attacher': element.find('attacher').text,
            'ispatch': element.get('ispatch', '0') == '1',
            'isobsolete': element.get('isobsolete', '0') == '1',
        })

    def process_comment(self, element):
        '''
        Stores commend data within this object.
        '''
        who_elm = element.find('who')
        if who_elm is None:
            if not self.anonymous:
                raise BugzillaNotPermitted(
                    'Could not load author from bugzilla', self.bug_id
                )
            else:
                who = ''
        else:
            who = who_elm.text

        when_elm = element.find('bug_when')
        if when_elm is None:
            if not self.anonymous:
                raise BugzillaNotPermitted(
                    'Could not load time of change from bugzilla', self.bug_id
                )
            else:
                when = None
        else:
            when = dateutil.parser.parse(when_elm.text)

        self.comments.append({
            'who': who,
            'bug_when': when,
            'private': (element.get('isprivate') == '1'),
            'thetext': element.find('thetext').text,
        })

    def process_flag(self, element):
        '''
        Store the given flag in the flag-list.
        '''
        flag = {}
        flag_attributes = ['name', 'id', 'type_id', 'status', 'setter',
                           'requestee']
        for attribute in flag_attributes:
            value = element.get(attribute)
            if value:
                flag[attribute] = value
        self.flags.append(flag)


class Bugzilla(WebScraper):
    '''
    Class for access to Novell bugzilla.
    '''

    def __init__(self, user, password, base='https://bugzilla.novell.com',
                 useragent=None, force_readonly=False, transport='pycurl'):
        super(Bugzilla, self).__init__(
            user, password, base, useragent, transport
        )
        self.force_readonly = force_readonly
        self.logger = logging.getLogger('suse.bugzilla')

    def possible_relogin(self, error):
        """
        Logins again to workaround possible bad cookies.
        """
        if (error.original and
                hasattr(error.original, 'code') and
                error.original.code == 502 and
                self.cookie_set):
            self.logger.warning(
                'Got 502 (Bad Gateway), clearing cookies and loging in again'
            )
            self.cookie_set = False
            self.browser.cookies.clear()
            self.login(force=True)
            return True
        return False

    def request(self, action, paramlist=None, **kwargs):
        '''
        Performs single request on a server (loads single page).
        '''
        try:
            return super(Bugzilla, self).request(
                action, paramlist, **kwargs
            )
        except WebScraperError as error:
            if self.possible_relogin(error):
                return super(Bugzilla, self).request(
                    action, paramlist, **kwargs
                )
            raise error

    def submit(self):
        '''
        Submits currently selected browser form.
        '''
        try:
            return super(Bugzilla, self).submit()
        except WebScraperError as error:
            if self.possible_relogin(error):
                return super(Bugzilla, self).submit()
            raise error

    def check_viewing_html(self):
        '''
        Checks whether the browser is in HTML viewing state.
        '''
        # pylint: disable=E1102
        if not self.viewing_html():
            raise BugzillaLoginFailed('Failed to load bugzilla form')

    def check_login(self):
        '''
        Check whether we're logged in.
        '''
        self.logger.info('Getting login page')
        self.request('index', GoAheadAndLogIn=1)

        return self._check_login()

    def _check_login(self):
        """
        Checks whether current page is logged in.
        """

        self.check_viewing_html()

        if self.browser.doc.select("//a[text()='Log out']").selector_list:
            self.logger.info('Already logged in')
            return True
        elif self.browser.doc.select(
                "//a[text()='Log\\xc2\\xa0out']"
        ).selector_list:
            self.logger.info('Already logged in')
            return True
        return False

    # pylint: disable=W0613
    def login(self, force=False):
        '''
        Login to Bugzilla using Access Manager.
        '''
        if self.check_login():
            return

        try:
            # Submit fake javascript form
            # pylint: disable=E1102
            self.browser.doc.choose_form(number=0)
            self.submit()
            # Find the login form
            # pylint: disable=E1102
            self.browser.doc.choose_form(number=0)

            self.browser.doc.set_input('Ecom_User_ID', self.user)
            self.browser.doc.set_input('Ecom_Password', self.password)
        except DataNotFound:
            raise BugzillaLoginFailed('Failed to parse HTML for login!')

        self.logger.info('Doing login')
        self.submit()

        text = self.browser.doc.unicode_body()

        # Check for error messages
        soup = BeautifulSoup(text, "lxml")
        for para in soup.find_all('p'):
            if 'error' in para['class']:
                raise BugzillaLoginFailed(para.text)

        # Emulate javascript redirect
        for script in soup.findAll('script'):
            for line in script.text.splitlines():
                line = line.strip()
                if line.startswith('top.location.href='):
                    path = line.split("'")[1]
                    newpath = urljoin(
                        self.browser.doc.url,
                        path
                    )
                    self.request(newpath)

        if not self.check_login():
            raise BugzillaLoginFailed(
                'Failed to verify login after successful login'
            )

    def _get_req_url(self, action):
        '''
        Formats request URL based on action.
        '''
        if action.startswith('http'):
            return action
        return self.base + '/' + action + '.cgi'

    def _handle_parse_error(self, bugid, data):
        '''
        Handles invalid output received from bugzilla.
        '''

        if data.find('Buglist Too Large') != -1:
            raise BuglistTooLarge('Buglist too large')

        if data.find('Bugzilla has suffered an internal error.'):
            raise BugzillaError('Bugzilla has suffered an internal error.')

        if data == '':
            raise BugzillaError('Received empty response from Bugzilla.')

        self.log_parse_error(bugid, data)

    def log_parse_error(self, bugid, data):
        '''
        Logs information about parse error.
        '''
        if data.startswith('<!DOCTYPE html'):
            self.logger.error(
                'Got HTML instead of from bugzilla for bug %s', bugid
            )
        else:
            self.logger.error(
                'Failed to parse XML response from bugzilla for bug %s: %s',
                bugid,
                traceback.format_exc()
            )

    def get_bug(self, bugid, retry=True):
        '''
        Returns Bug object based on data received from bugzilla.

        Returns None in case of failure.
        '''
        result = self.get_bugs([bugid], retry)
        if result:
            return result[0]
        return None

    def get_bugs(self, ids, retry=True, permissive=False, store_errors=False):
        '''
        Returns Bug objects based on data received from bugzilla for each bug
        ID.

        Returns empty list in case of some problems.
        '''
        # Generate request query
        req = [('id', bugid) for bugid in ids if bugid is not None]
        req += [('ctype', 'xml'), ('excludefield', 'attachmentdata')]

        # Download data
        data = self.request('show_bug', paramlist=req)

        # Fixup XML errors bugzilla produces
        data = escape_xml_text(data.unicode_body())

        # Parse XML
        try:
            # pylint: disable=no-member
            parser = ElementTree.XMLParser(recover=True)
            # pylint: disable=no-member
            response_et = ElementTree.fromstring(data.encode('utf-8'), parser)
        except SyntaxError:
            self._handle_parse_error(
                ','.join([str(bugid) for bugid in ids]),
                data
            )
            return []
        try:
            bugs = []
            for bug in response_et.findall('bug'):
                try:
                    bugs.append(Bug(bug, self.anonymous))
                except BugzillaError as exc:
                    if store_errors:
                        bugs.append(exc)
                    if permissive:
                        self.logger.error(exc)
                    else:
                        raise exc
            return bugs
        except BugzillaNotPermitted as exc:
            if retry and not self.anonymous:
                self.logger.error("%s - login and retry", exc)
                self.login()
                return self.get_bugs(ids, False, permissive)
            raise exc

    def do_search(self, params):
        '''
        Performs search and returns list of IDs.
        '''
        req = [('ctype', 'atom')] + params
        self.logger.info('Doing bugzilla search: %s', req)
        response = self.request('buglist', paramlist=req)
        data = escape_xml_text(response.unicode_body())
        try:
            # pylint: disable=no-member
            parser = ElementTree.XMLParser(recover=True)
            # pylint: disable=no-member
            response_et = ElementTree.fromstring(data.encode('utf-8'), parser)
        except SyntaxError:
            self._handle_parse_error('recent', data)
            return []

        id_query = '{http://www.w3.org/2005/Atom}id'
        entry_query = '{http://www.w3.org/2005/Atom}entry'

        bugs = [
            bug.find(id_query).text for bug in response_et.findall(entry_query)
        ]

        # Strip http://bugzilla.novell.com/show_bug.cgi?id=
        return [int(bugid[bugid.find("?id=") + 4:]) for bugid in bugs]

    def get_recent_bugs(self, startdate):
        '''
        Returns lis of bugs changed since start date.
        '''
        return self.do_search([
            ('chfieldto', 'Now'),
            ('chfieldfrom', startdate.strftime('%Y-%m-%d %H:%M:%S +0000'))
        ])

    def get_opensec_bugs(self):
        '''
        Searches for security related bugs
        '''
        return self.do_search([
            ('short_desc', '^VUL-[0-9]'),
            ('query_format', 'advanced'),
            ('bug_status', 'NEW'),
            ('bug_status', 'ASSIGNED'),
            ('bug_status', 'NEEDINFO'),
            ('bug_status', 'REOPENED'),
            ('component', 'Incidents'),
            ('product', 'SUSE Security Incidents'),
            ('short_desc_type', 'regexp')
        ])

    def get_recent_secbugs(self, startdate):
        '''
        Returns lis of security bugs changed since start date.
        '''
        return self.do_search([
            ('short_desc', '^VUL-[0-9]'),
            ('query_format', 'advanced'),
            ('short_desc_type', 'regexp'),
            ('chfieldto', 'Now'),
            ('component', 'Incidents'),
            ('product', 'SUSE Security Incidents'),
            ('chfieldfrom', startdate.strftime('%Y-%m-%d %H:%M:%S +0000'))
        ])

    def get_openl3_bugs(self):
        '''
        Searches for bugs with openL3 in whiteboard.
        '''
        return self.do_search([
            ('status_whiteboard_type', 'allwordssubstr'),
            ('query_format', 'advanced'),
            ('status_whiteboard', 'openL3')
        ])

    def get_l3_summary_bugs(self):
        '''
        Searches for open bugs with L3: in summary.
        '''
        return self.do_search([
            ('short_desc', 'L3:'),
            ('query_format', 'advanced'),
            ('bug_status', 'NEW'),
            ('bug_status', 'ASSIGNED'),
            ('bug_status', 'NEEDINFO'),
            ('bug_status', 'REOPENED'),
            ('short_desc_type', 'allwordssubstr')
        ])

    def get_sr(self, bugid):
        '''
        Black magic to obtain SR ids from bugzilla.
        '''
        # Load the form
        self.logger.info('Loading bug page for %d', bugid)
        self.request('show_bug', id=bugid)

        self.check_viewing_html()

        # Find link containing SR ids
        link = self.browser.doc.select(
            "//a[text()='Report View']"
        ).selector_list
        if not link:
            return []

        # Split parts (URL encoded)
        urlpart = [x for x in link.url.split('%26') if x[:7] == 'lsMSRID']

        if not urlpart:
            return []

        # Find SR ids
        match = SR_MATCH.findall(urlpart[0])

        # Convert to integers
        return [int(x) for x in match]

    def load_update_form(self, bugid):
        """
        Selects form for bug update.
        """
        if self.anonymous:
            raise BugzillaUpdateError('No updates in anonymous mode!')

        # Load the form
        self.logger.info('Loading bug form for %d', bugid)
        response = self.request('show_bug', id=bugid)
        data = response.unicode_body()
        if 'You are not authorized to access bug' in data:
            raise BugzillaNotPermitted(
                'You are not authorized to access bug #%d.' % bugid
            )

        self.check_viewing_html()

        # Find the form
        try:
            # pylint: disable=E1102
            self.browser.doc.choose_form(xpath="//form[@name='changeform']")
        except DataNotFound:
            raise BugzillaUpdateError('Failed to parse HTML to update bug!')

    def update_bug(self, bugid, callback=None, callback_param=None,
                   whiteboard_add=None, whiteboard_remove=None, **kwargs):
        '''
        Updates bugzilla.
        '''
        self.load_update_form(bugid)

        changes = False

        # Do not add ourselves to CC when setting whiteboard
        if ((whiteboard_add is not None or whiteboard_remove is not None) and
                'addselfcc' not in kwargs):
            kwargs['addselfcc'] = []

        # Set parameters
        for k in kwargs:
            val = kwargs[k]
            if isinstance(val, text_type):
                val = val.encode('utf-8')
            try:
                self.browser.doc.set_input(k, val)
            except DataNotFound:
                if k not in IGNORABLE_FIELDS:
                    raise
            changes = True

        # Callback can adjust data on fly
        if callback is not None:
            changes |= callback(self.browser, callback_param)

        # Whiteboard manipulations
        if whiteboard_add is not None or whiteboard_remove is not None:
            changes |= self._update_bug_whiteboard(
                whiteboard_remove,
                whiteboard_add
            )

        # Retrun on no changes
        if not changes or self.force_readonly:
            return

        # Submit
        response = self.submit()
        data = webscraper_safely(response.read)
        if 'Mid-air collision!' in data:
            raise BugzillaUpdateError('Mid-air collision!')
        if 'reason=invalid_token' in data:
            raise BugzillaUpdateError('Suspicious Action')
        if 'Changes submitted for' not in data:
            raise BugzillaUpdateError('Unknown error while submitting form')

    def _update_bug_whiteboard(self, remove, add):
        '''
        Callback for changing bug whiteboard.
        '''
        whiteboard = self.browser.doc.form_fields()['status_whiteboard']

        if remove is not None and remove in whiteboard:
            whiteboard = whiteboard.replace(remove, '')

        if add is not None and add not in whiteboard:
            whiteboard = '%s %s' % (whiteboard, add)

        current_wb = self.browser.doc.form_fields()['status_whiteboard']
        changes = (current_wb != whiteboard)

        self.browser.doc.set_input('status_whiteboard', whiteboard)

        return changes


class APIBugzilla(Bugzilla):
    '''
    Wrapper class to use apibugzilla.suse.com.
    '''

    def __init__(self, user, password, base='https://apibugzilla.suse.com',
                 useragent=None, force_readonly=False, transport='pycurl'):
        super(APIBugzilla, self).__init__(
            user, password, base, useragent, transport=transport
        )
        self.force_readonly = force_readonly
        # Use normal Bugzilla for anonymous access
        if self.anonymous and 'suse.com' in base:
            self.base = 'https://bugzilla.suse.com'
        else:
            self.browser.setup(
                userpwd="{user}:{password}".format(user=user,
                                                   password=password)
            )

    def login(self, force=False):
        '''
        Checks login to Bugzilla using HTTP authentication.
        '''
        self.logger.info('Getting login page')
        self.request('index', GoAheadAndLogIn=1)

        if not self._check_login():
            raise BugzillaLoginFailed('Failed to login to bugzilla')


class DjangoBugzilla(APIBugzilla):
    '''
    Adds Django specific things to bugzilla class.
    '''

    def _log_parse_error(self, bugid, data):
        '''
        Sends email to admin on error.
        '''
        from django.core.mail import mail_admins
        subject = 'Error while fetching %s' % bugid
        message = 'Exception:\n\n%s\n\n\nData:\n\n%s\n' % (
            traceback.format_exc(),
            data,
        )
        mail_admins(subject, message, fail_silently=True)
        super(DjangoBugzilla, self).log_parse_error(bugid, data)

    def login(self, force=False):
        """
        Login with caching cookies in Django.
        """
        from django.core.cache import cache
        if force:
            cookies = None
        else:
            cookies = cache.get('bugzilla-access-cookies')

        if cookies is None:
            super(DjangoBugzilla, self).login(force)
            cache.set('bugzilla-access-cookies', self.get_cookies())
        else:
            self.set_cookies(cookies)


def get_django_bugzilla(transport='pycurl'):
    '''
    Returns logged in bugzilla object. Access cookies are stored in django
    cache.
    '''
    from django.conf import settings
    force_readonly = (
        hasattr(settings, 'BUGZILLA_FORCE_READONLY') and
        settings.BUGZILLA_FORCE_READONLY
    )
    bugzilla = DjangoBugzilla(
        settings.BUGZILLA_USERNAME,
        settings.BUGZILLA_PASSWORD,
        useragent=settings.EMAIL_SUBJECT_PREFIX.strip('[] '),
        force_readonly=force_readonly,
        transport=transport
    )

    # Check for anonymous access
    if settings.BUGZILLA_USERNAME == '':
        return bugzilla

    bugzilla.login()

    return bugzilla
