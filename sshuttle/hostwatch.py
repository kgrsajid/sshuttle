import time
import socket
import re
import select
import errno
import os
import sys
import platform

import subprocess as ssubprocess
import sshuttle.helpers as helpers
from sshuttle.helpers import log, debug1, debug2, debug3, get_env

POLL_TIME = 60 * 15
NETSTAT_POLL_TIME = 30
CACHEFILE = os.path.expanduser('~/.sshuttle.hosts')


hostnames = {}
queue = {}
try:
    null = open(os.devnull, 'wb')
except IOError:
    _, e = sys.exc_info()[:2]
    log('warning: %s' % e)
    null = os.popen("sh -c 'while read x; do :; done'", 'wb', 4096)


def _is_ip(s):
    return re.match(r'\d+\.\d+\.\d+\.\d+$', s)


def write_host_cache():
    tmpname = '%s.%d.tmp' % (CACHEFILE, os.getpid())
    try:
        f = open(tmpname, 'wb')
        for name, ip in sorted(hostnames.items()):
            f.write(('%s,%s\n' % (name, ip)).encode("ASCII"))
        f.close()
        os.chmod(tmpname, 384)  # 600 in octal, 'rw-------'
        os.rename(tmpname, CACHEFILE)
    finally:
        try:
            os.unlink(tmpname)
        except BaseException:
            pass


def read_host_cache():
    try:
        f = open(CACHEFILE)
    except IOError:
        _, e = sys.exc_info()[:2]
        if e.errno == errno.ENOENT:
            return
        else:
            raise
    for line in f:
        words = line.strip().split(',')
        if len(words) == 2:
            (name, ip) = words
            name = re.sub(r'[^-\w\.]', '-', name).strip()
            ip = re.sub(r'[^0-9.]', '', ip).strip()
            if name and ip:
                found_host(name, ip)


def found_host(name, ip):
    hostname = re.sub(r'\..*', '', name)
    hostname = re.sub(r'[^-\w\.]', '_', hostname)
    if (ip.startswith('127.') or ip.startswith('255.') or
            hostname == 'localhost'):
        return

    if hostname != name:
        found_host(hostname, ip)

    oldip = hostnames.get(name)
    if oldip != ip:
        hostnames[name] = ip
        debug1('Found: %s: %s' % (name, ip))
        sys.stdout.write('%s,%s\n' % (name, ip))
        write_host_cache()


def _check_etc_hosts():
    debug2(' > hosts')
    for line in open('/etc/hosts'):
        line = re.sub(r'#.*', '', line)
        words = line.strip().split()
        if not words:
            continue
        ip = words[0]
        names = words[1:]
        if _is_ip(ip):
            debug3('<    %s %r' % (ip, names))
            for n in names:
                check_host(n)
                found_host(n, ip)


def _check_revdns(ip):
    debug2(' > rev: %s' % ip)
    try:
        r = socket.gethostbyaddr(ip)
        debug3('<    %s' % r[0])
        check_host(r[0])
        found_host(r[0], ip)
    except (socket.herror, UnicodeError):
        pass


def _check_dns(hostname):
    debug2(' > dns: %s' % hostname)
    try:
        ip = socket.gethostbyname(hostname)
        debug3('<    %s' % ip)
        check_host(ip)
        found_host(hostname, ip)
    except (socket.gaierror, UnicodeError):
        pass


def _check_netstat():
    debug2(' > netstat')
    argv = ['netstat', '-n']
    try:
        p = ssubprocess.Popen(argv, stdout=ssubprocess.PIPE, stderr=null,
                              env=get_env())
        content = p.stdout.read().decode("ASCII")
        p.wait()
    except OSError:
        _, e = sys.exc_info()[:2]
        log('%r failed: %r' % (argv, e))
        return

    for ip in re.findall(r'\d+\.\d+\.\d+\.\d+', content):
        debug3('<    %s' % ip)
        check_host(ip)


def check_host(hostname):
    if _is_ip(hostname):
        _enqueue(_check_revdns, hostname)
    else:
        _enqueue(_check_dns, hostname)


def _enqueue(op, *args):
    t = (op, args)
    if queue.get(t) is None:
        queue[t] = 0


def _stdin_still_ok(timeout):
    r, _, _ = select.select([sys.stdin.fileno()], [], [], timeout)
    if r:
        b = os.read(sys.stdin.fileno(), 4096)
        if not b:
            return False
    return True


def hw_main(seed_hosts, auto_hosts):
    helpers.logprefix = 'HH: '

    debug1('Starting hostwatch with Python version %s'
           % platform.python_version())

    for h in seed_hosts:
        check_host(h)

    if auto_hosts:
        read_host_cache()
        _enqueue(_check_etc_hosts)
        _enqueue(_check_netstat)
        check_host('localhost')
        check_host(socket.gethostname())

    while 1:
        now = time.time()
        for t, last_polled in list(queue.items()):
            (op, args) = t
            if not _stdin_still_ok(0):
                break
            maxtime = POLL_TIME
            if op == _check_netstat:
                maxtime = NETSTAT_POLL_TIME
            if now - last_polled > maxtime:
                queue[t] = time.time()
                op(*args)
            try:
                sys.stdout.flush()
            except IOError:
                break

        # FIXME: use a smarter timeout based on oldest last_polled
        if not _stdin_still_ok(1):
            break
