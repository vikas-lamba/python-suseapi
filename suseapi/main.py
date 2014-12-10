from __future__ import print_function

import sys
from xdg.BaseDirectory import load_first_config
from pprint import pformat
from argparse import ArgumentParser
from copy import deepcopy

from suseapi.userinfo import UserInfo


class ErrorMessage(Exception):
    pass


def main():
    try:
        realmain(load_first_config, {
            'lookup-user': LookupUser,
        })
    except ErrorMessage as e:
        print(e, file=sys.stderr)
        sys.exit(1)



def get_parser():
    parser = ArgumentParser()
    subparser = parser.add_subparsers(dest="cmd")

    lup = subparser.add_parser(
        "lookup-user",
        description="Look up a user in LDAP",
    )
    lup.add_argument("--by", type=str, default='smart-uid')
    lup.add_argument("value", nargs=1, type=str)

    return parser


class Command(object):
    def __init__(self, args, config):
        self.args = args
        self.config = config

        self.run()

    def println(self, line):
        print(line, file=sys.stdout)

    def run(self):
        raise NotImplementedError


class LookupUser(Command):
    def run(self):
        self.println(pformat(self.search()))

    def search(self):
        userinfo = UserInfo(
            self.config['ldap.server'],
            self.config['ldap.base']
        )
        if self.args.by == "smart-uid":
            return userinfo.search_uid(self.args.value[0], [])

        return userinfo.search_by(self.args.by, self.args.value[0])


def realmain(config_loader, commands):
    parser = get_parser()
    args = parser.parse_args(sys.argv[1:])

    filename = config_loader("suseapi")
    if not filename:
        raise ErrorMessage("Missing config file")

    # parse like Xdefaults file
    config = dict([
        (key.strip(), val.strip()) for key, dummy, val
        in [x.partition(":") for x in open(filename).readlines()]
    ])

    commands[args.cmd](args, config)