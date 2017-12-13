#! /usr/bin/env python
"""
Selection of commonly-used correlator control functions. This is the top-level file used to communicate with correlators.

Author: Jason Manley

Revisions:
2017-12-13: Moved Correlator class to new file (correlator.py) to avoid circular relative imports.
2012-06-01: JRM Object-wide spead itemgroup and transmitter.
2012-01-11: JRM Cleanup of SPEAD metadata to match new documentation.
2011-07-06: PVP New functions to read/write/pulse bitfields within registers. Remove a bit of duplicate code for doing that.
2011-06-23: JMR Moved all snapshot stuff into new file (snap.py)
2011-05-xx: JRM change ant,pol handling to be arbitrary strings.
                deprecated get_ant_location. Replaced by get_ant_str_location
                updates to adc_amplitudes_get
                added rf_level low warning
                spead metadata changes: antenna mapping and baseline ordering. no more cross-pol ordering.
                new functions: fr_delay_set_all to set all fringe and delay rates in one go. does check to ensure things loaded correctly.
2011-04-20: JRM Added xeng status call
                Mods to check_all
                acc_time_set now resets counters (vaccs produce errors when resyncing)
2011-04-04: JRM Don't write to config file anymore
                Cleanup of RF frontend stuff
                get_adc_snapshot with trigger support
2011-02-11: JRM Issue SPEAD data descriptors for interleaved case.
2011-01-02: JRM Removed requirement for stats package (basic mode calc built-in).
                Bugfix to arm and check_feng_clks logic (waiting for half second).
2011-01-03: JRM Modified check_fpga_comms to limit random number 2**32.
2010-12-02  JRM Added half-second wait for feng clk check's PPS count
2010-11-26: JRM Added longterm ppc count check in feng clock check.
2010-11-22: JRM corr-0.6.0 now with support for fengines with 10gbe cores instead of xaui links.
                Mods to fr_delay calcs for fine delay.
                spead, pcnt and mcnt from time functions now account for wrapping counters.
2010-10-18: JRM initialise function added.
                Fix to SPEAD metadata issue when using iADCs.
2010-08-05: JRM acc_len_set -> acc_n_set. acc_let_set now in seconds.
2010-06-28  JRM Port to use ROACH based F and X engines.
                Changed naming convention for function calls.
2010-04-02  JCL Removed base_ant0 software register from Xengines, moved it to Fengines, and renamed it to use ibob_addr0 and ibob_data0.
                New function write_ibob().
                Check for VACC errors.
2010-01-06  JRM Added gbe_out enable to X engine control register
2009-12-14  JRM Changed snap_x to expect two kinds of snap block, original simple kind, and new one with circular capture, which should have certain additional registers (wr_since_trig).
2009-12-10  JRM Started adding SPEAD stuff.
2009-12-01  JRM Added check for loopback mux sync to, and fixed bugs in, loopback_check_mcnt.
                Changed all "check" functions to just return true/false for global system health. Some have "verbose" option to print more detailed errors.
                Added loopback_mux_rst to xeng_ctrl
2009-11-06  JRM Bugfix snap_x offset triggering.
2009-11-04  JRM Added ibob_eq_x.
2009-10-29  JRM Bugfix snap_x.
2009-06-26  JRM UNDER CONSTRUCTION.
"""

import time
import sys
import numpy
import logging
import struct
import socket
import os

import construct
import spead64_48 as spead

__all__ = [
            "statsmode",
            "ip2str",
            "write_masked_register",
            "read_masked_register",
            "pulse_masked_register",
            "log_runtimeerror",
            "non_blocking_request",
            "dbm_to_dbuv",
            "dbuv_to_dbm",
            "v_to_dbuv",
            "dbuv_to_v",
            "dbm_to_v",
            "v_to_dbm",
            "CORR_MODE_WB",
            "CORR_MODE_NB",
            "CORR_MODE_DDC"
           ]

CORR_MODE_WB = 'wbc'
CORR_MODE_NB = 'nbc'
CORR_MODE_DDC = 'ddc'

def statsmode(inlist):
    """Very rudimentarily calculates the mode of an input list. Only returns one value, the first mode. Can't deal with ties!"""
    value=inlist[0]
    count=inlist.count(value)
    for i in inlist:
        if inlist.count(i) > count:
            value = i
            count = inlist.count(i)
    return value

def ip2str(pkt_ip, verbose = False):
    """
    Returns a dot notation IPv4 address given a 32-bit number.
    """
    ip_4 = (pkt_ip & ((2**32) - (2**24))) >> 24
    ip_3 = (pkt_ip & ((2**24) - (2**16))) >> 16
    ip_2 = (pkt_ip & ((2**16) - (2**8)))  >> 8
    ip_1 = (pkt_ip & ((2**8)  - (2**0)))  >> 0
    ipstr = '%i.%i.%i.%i' % (ip_4, ip_3, ip_2, ip_1)
    if verbose:
        print 'IP(%i) decoded to:' % pkt_ip, ipstr
    return ipstr

def write_masked_register(device_list, bitstruct, names = None, **kwargs):
    """
    Modify arbitrary bitfields within a 32-bit register, given a list of devices that offer the write_int interface - should be KATCP FPGA devices.
    """
    # lazily let the read function check our arguments
    currentValues = read_masked_register(device_list, bitstruct, names, return_dict = False)
    wv = []
    pulse_keys = []
    for c in currentValues:
        for key in kwargs:
            if not c.__dict__.has_key(key):
                raise RuntimeError('Attempting to write key %s but it doesn\'t exist in bitfield.' % key)
            if kwargs[key] == 'pulse':
                if pulse_keys.count(key) == 0: pulse_keys.append(key)
            else:
                c.__dict__[key] = (not c.__dict__[key]) if (kwargs[key] == 'toggle') else kwargs[key]
        bitstring = bitstruct.build(c)
        unpacked = struct.unpack('>I', bitstring)
        wv.append(unpacked[0])
    for d, device in enumerate(device_list):
        device.write_int(c.register_name, wv[d])
    # now pulse any that were asked to be pulsed
    if len(pulse_keys) > 0:
        #print 'Pulsing keys from write_... :(', pulse_keys
        pulse_masked_register(device_list, bitstruct, pulse_keys)

def read_masked_register(device_list, bitstruct, names = None, return_dict = True):
    """
    Read a 32-bit register from each of the devices (anything that provides the read_uint interface) in the supplied list and apply the given construct.BitStruct to the data.
    A list of Containers or dictionaries is returned, indexing the same as the supplied list.
    """
    if bitstruct == None:
        return
    if bitstruct.sizeof() != 4:
        raise RuntimeError('Function can only work with 32-bit bitfields.')
    registerNames = names
    if registerNames == None:
        registerNames = []
        for d in device_list: registerNames.append(bitstruct.name)
    if len(registerNames) !=  len(device_list):
        raise RuntimeError('Length of list of register names does not match length of list of devices given.')
    rv = []
    for d, device in enumerate(device_list):
        vuint = device.read_uint(registerNames[d])
        rtmp = bitstruct.parse(struct.pack('>I', vuint))
        rtmp.raw = vuint
        rtmp.register_name = registerNames[d]
        if return_dict: rtmp = rtmp.__dict__
        rv.append(rtmp)
    return rv

def pulse_masked_register(device_list, bitstruct, fields):
    """
    Pulse a boolean var somewhere in a masked register.
    The fields argument is a list of strings representing the fields to be pulsed. Does NOT check Flag vs BitField, so make sure!
    http://stackoverflow.com/questions/1098549/proper-way-to-use-kwargs-in-python
    """
    zeroKwargs = {}
    oneKwargs = {}
    for field in fields:
      zeroKwargs[field] = 0
      oneKwargs[field] = 1
    #print zeroKwargs, '|', oneKwargs
    write_masked_register(device_list, bitstruct, **zeroKwargs)
    write_masked_register(device_list, bitstruct, **oneKwargs)
    write_masked_register(device_list, bitstruct, **zeroKwargs)

def log_runtimeerror(logger, err):
    """Have the logger log an error and then raise it.
    """
    logger.error(err)
    raise RuntimeError(err)

def non_blocking_request(fpgas, timeout, request, request_args):
    """Make a non-blocking request to one or more FPGAs, using the Asynchronous FPGA client.
    """
    import Queue, threading
    verbose = False
    reply_queue = Queue.Queue(maxsize=len(fpgas))
    requests = {}
    # reply callback
    def reply_cb(host, request_id):
        #if not requests.has_key(host):
        #    raise RuntimeError('Rx reply(%s) from host(%s), did not send request?' % (request_id, host))
        ## is the reply queue full?
        #if reply_queue.full():
        #    raise RuntimeError('Rx reply(%s) from host(%s), reply queue is full?' % (request_id, host))
        if verbose: print 'Reply(%s) from host(%s)' % (request_id, host); sys.stdout.flush()
        reply_queue.put_nowait([host, request_id])
    # start the requests
    if verbose: print 'Send request(%s) to %i hosts.' % (request, len(fpgas))
    lock = threading.Lock()
    for f in fpgas:
        lock.acquire()
        r = f._nb_request(request, None, reply_cb, *request_args)
        requests[r['host']] = [r['request'], r['id']]
        lock.release()
        if verbose: print 'Request \'%s\' id(%s) to host(%s)' % (r['request'], r['id'], r['host']); sys.stdout.flush()
    # wait for replies from the requests
    replies = {}
    timedout = False
    done = False
    while (not timedout) and (not done):
        try:
            it = reply_queue.get(block = True, timeout = timeout)
        except:
            timedout = True
            break
        replies[it[0]] = it[1]
        if len(replies) == len(fpgas): done = True
    if timedout and verbose:
        print replies
        print "non_blocking_request timeout after %is." % timeout; sys.stdout.flush()
    rv = {}
    for f in fpgas:
        frv = {}
        try:
            request_id = replies[f.host]
        except:
            print replies
            sys.stdout.flush()
            raise KeyError('Didn\'t get a reply for FPGA \'%s\' so the request \'%s\' probably didn\'t complete.' % (f.host, request))
        reply, informs = f._nb_get_request_result(request_id)
        frv['request'] = requests[f.host][0]
        frv['reply'] = reply.arguments[0]
        frv['reply_args'] = reply.arguments
        informlist = []
        for inf in informs:
            informlist.append(inf.arguments)
        frv['informs'] = informlist
        rv[f.host] = frv
        f._nb_pop_request_by_id(request_id)
    return (not timedout), (rv)

def dbm_to_dbuv(dbm):
    return dbm+107

def dbuv_to_dbm(dbuv):
    return dbuv-107

def v_to_dbuv(v):
    return 20*numpy.log10(v*1e6)

def dbuv_to_v(dbuv):
    return (10**(dbuv/20.))/1e6

def dbm_to_v(dbm):
    return numpy.sqrt(10**(dbm/10.)/1000*50)

def v_to_dbm(v):
    return 10*numpy.log10(v*v/50.*1000)
