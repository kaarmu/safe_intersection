#! /usr/bin/env python3.9

# Plain python imports
import numpy as np
import secrets
import json
from threading import RLock
from math import pi, ceil, floor
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps

import rospy
from ltms.msg import NamedBytes
from ltms.srv import Connect, Notify, Reserve
from svea_msgs.msg import VehicleState as StateMsg

import hj_reachability as hj
import hj_reachability.shapes as shp
from ltms_util import Solver, create_chaos
from ltms_util.debuggable_lock import *
from nats_ros_connector.nats_manager import NATSManager

def load_param(name, value=None):
    """Function used to get parameters from ROS parameter server

    :param name: name of the parameter
    :type name: string
    :param value: default value of the parameter, defaults to None
    :type value: _type_, optional
    :return: value of the parameter
    :rtype: _type_
    """
    if value is None:
        assert rospy.has_param(name), f'Missing parameter "{name}"'
    return rospy.get_param(name, value)

def closest_subzero(vf, idx):
    """
    Find the index of the closest sub-zero element to the given index in a 5D numpy array.
    
    Parameters:
    vf (np.ndarray): A 5D numpy array.
    idx (tuple): A 5-tuple representing the index to find the closest sub-zero element to.
    
    Returns:
    tuple: A 5-tuple `jdx` corresponding to the index of the closest sub-zero element.
    """
    # Ensure idx is a valid 5-tuple.
    if len(idx) != len(vf.shape):
        raise ValueError("idx must be a 5-tuple")
    
    # Get the indices of all sub-zero elements in `vf`.
    subzero_indices = np.argwhere(vf < 0)
    
    # If there are no sub-zero elements, return None or raise an error.
    if len(subzero_indices) == 0:
        raise ValueError("No sub-zero elements found in the array")
    
    # Calculate the distance of each sub-zero index to the given index `idx`.
    distances = np.linalg.norm(subzero_indices - np.array(idx), axis=1)
    
    # Find the index of the closest sub-zero element.
    closest_idx = np.argmin(distances)
    jdx = tuple(subzero_indices[closest_idx])
    
    return jdx, distances[closest_idx]

def service_wrp(srv_cls, method=False):
    Req  = srv_cls._request_class
    Resp = srv_cls._response_class
    cond = lambda args, kwds: (not kwds 
                               and len(args) == 1 
                               and isinstance(args[0], Req))
    def decorator(f):
        if method:
            @wraps(f)
            def wrapper(self, *args, **kwds):
                req = (args[0] if cond(args, kwds) else Req(*args, **kwds))
                resp = Resp()
                f(self, req, resp)
                return resp
        else:
            @wraps(f)
            def wrapper(*args, **kwds):
                req = (args[0] if cond(args, kwds) else Req(*args, **kwds))
                resp = Resp()
                resp = Resp()
                f(req, resp)
                return resp
        return wrapper
    return decorator

def around(l, e, n):
    if e not in l: return []
    i = l.index(e)
    N = len(l)
    return [l[(i+j) % N] for j in range(-n, n+1)]

_LOCATIONS = ['left', 'top', 'right', 'bottom']
_PERMITTED_ROUTES = {
    (_entry, _exit): ('full',)
    for _entry in _LOCATIONS
    for _exit in set(_LOCATIONS) - {_entry}
}

class Server:

    AVOID_MARGIN = 0.4
    TIME_HORIZON = 15
    TIME_STEP = 0.2

    MAX_WINDOW_ENTRY = 2

    SESSION_TIMEOUT = timedelta(seconds=30)
    TRANSIT_TIME = 15 # [s] made up, roughly accurate
    COMPUTE_TIME = 10 # [s] made up, roughly accurate

    ENTRY_LOCATIONS = _LOCATIONS + ['init']
    EXIT_LOCATIONS = _LOCATIONS
    LOCATIONS = _LOCATIONS + ['full', 'init']
    PERMITTED_ROUTES = _PERMITTED_ROUTES | {
        ('init', _exit): ('full',)
        for _exit in 'left'.split()
    }

    def __init__(self):

        ## Initialize node

        rospy.init_node(self.__class__.__name__, log_level=rospy.DEBUG)

        ## Load parameters

        self.NAME = load_param('~name')

        self.DATA_DIR = load_param('~data_dir')
        self.DATA_DIR = Path(self.DATA_DIR)

        self.MODEL = load_param('~model', 'Bicycle4D')
        self.MODEL = vars(hj.systems)[self.MODEL]

        self.GRID_SHAPE = load_param('~grid_shape', [31, 31, 25, 7])
        self.GRID_SHAPE = tuple(map(int, self.GRID_SHAPE))

        self.MIN_BOUNDS = load_param('~min_bounds', [-1.5, -1.5, -np.pi, +0.0])
        self.MIN_BOUNDS = np.array([eval(x) if isinstance(x, str) else x for x in self.MIN_BOUNDS])
        
        self.MAX_BOUNDS = load_param('~max_bounds', [+1.5, +1.5, +np.pi, +0.6])
        self.MAX_BOUNDS = np.array([eval(x) if isinstance(x, str) else x for x in self.MAX_BOUNDS])

        self.nats_mgr = NATSManager()

        ## Create simulators, models, managers, etc.

        self.grid = hj.Grid.from_lattice_parameters_and_boundary_conditions(hj.sets.Box(self.MIN_BOUNDS, self.MAX_BOUNDS),
                                                                            self.GRID_SHAPE, periodic_dims=2)

        self.solver = Solver(grid=self.grid, 
                             time_step=self.TIME_STEP,
                             time_horizon=self.TIME_HORIZON,
                             accuracy='low',
                             dynamics=dict(cls=self.MODEL,
                                           min_steer=-pi * 5/4, 
                                           max_steer=+pi * 5/4,
                                           min_accel=-0.5, 
                                           max_accel=+0.5),
                             interactive=False)

        self.environment = self.load_environment()
        self.offline_passes = self.load_offline_analyses()

        self.sessions_lock = debuggable_lock('sessions_lock', RLock())
        self.sessions = {}

        def clean_sessions_tmr(event):
            now = datetime.now().replace(microsecond=0)
            with self.sessions_lock('clean_sessions_tmr'):
                for sid, sess in self.get_sessions:
                    if sess['session_timeout'] < now:
                        del self.sessions[sid]
        rospy.Timer(rospy.Duration(2), clean_sessions_tmr)

        ## Advertise services

        self.Connect = self.nats_mgr.new_service('/server/connect', Connect, self.connect_srv)
        self.Notify = self.nats_mgr.new_service('/server/notify', Notify, self.notify_srv)
        self.Resere = self.nats_mgr.new_service('/server/reserve', Reserve, self.reserve_srv)
        
        self.Limits = self.nats_mgr.new_publisher(f'/server/limits', NamedBytes, queue_size=5)
        self.State = self.nats_mgr.new_subscriber(f'/server/state', StateMsg, self.state_cb)

        ## Node initialized

        rospy.loginfo(f'{self.__class__.__name__} initialized!')

    def load_environment(self):
        out = {}
        for loc in self.LOCATIONS:
            filename = self.DATA_DIR / f'G{self.solver.code_grid}-{loc}.npy'
            if rospy.is_shutdown():
                break
            elif filename.exists():
                out[loc] = np.load(filename, allow_pickle=True)
                print(f'Loading {filename}')
            else:
                out.update(create_chaos(self.grid, loc))
                print(f'Saving {filename}')
                np.save(filename, out[loc], allow_pickle=True)
        print('Environment done.')
        return out

    def load_offline_analyses(self):
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        out = {}
        for (entry, exit), locs in self.PERMITTED_ROUTES.items():
            code = (f'G{self.solver.code_grid}'
                    f'D{self.solver.code_dynamics}'
                    f'T{self.solver.code_time}')
            filename = self.DATA_DIR / f'{code}-pass1-{entry}-{exit}.npy'
            if rospy.is_shutdown():
                break
            elif filename.exists():
                print(f'Loading {filename}')
                out[entry, exit] = np.load(filename, allow_pickle=True)
            else:
                constraints = shp.union(*[self.environment[loc] for loc in locs])

                output = self.solver.run_analysis('pass1',
                                                  exit=self.environment[exit],
                                                  constraints=constraints)
                
                print(f'Saving {filename}')
                np.save(filename, output['pass1'], allow_pickle=True)
                out[entry, exit] = output['pass1']
        
        print('Offline analyses done.')
        return out
    
    @property
    def iter_sessions(self):
        with self.sessions_lock('iter_sessions'):
            yield from self.sessions.items()

    @property
    def get_sessions(self):
        with self.sessions_lock('get_sessions'):
            return list(self.sessions.items())
    
    def sessions_add(self, sid, dict1=None, **kwds):
        assert bool(dict1 is not None) ^ bool(kwds), 'Use either dict1 or keywords'
        with self.sessions_lock('sessions_add'):
            self.sessions[sid] = kwds if dict1 is None else dict1

    def select_session(self, sid):
        with self.sessions_lock('select_session'):
            if sid in self.sessions:
                yield self.sessions[sid]

    @property
    def get_reservations(self):
        return [(sid, sess['reservation']) for sid, sess in self.iter_sessions if sess['reserved']]
    
    def add_reservation(self, sid, dict1=None, **kwds):
        assert bool(dict1 is not None) ^ bool(kwds), 'Use either dict1 or keywords'
        with self.sessions_lock('add_reservation'):
            for sess in self.select_session(sid):
                sess['reservation'] = kwds if dict1 is None else dict1
                sess['reserved'] = True
    
    @service_wrp(Connect, method=True)
    def connect_srv(self, req, resp):

        usr_id = req.usr_id
        its_id = f'its_{secrets.token_hex(4)}'

        rospy.logdebug(f'# Connecting {req.usr_id} will get here at {req.arrival_time}')

        arrival_time = datetime.fromisoformat(req.arrival_time)
        session_timeout = arrival_time + self.SESSION_TIMEOUT

        latest_reserve_time = self.latest_reserve_time(usr_id, arrival_time)
        now = datetime.now()

        if latest_reserve_time < now:
            rospy.loginfo(f'Invalid connection request from {usr_id}: Earlier than HPV.')
            resp.transit_time = -1000
            return

        rospy.logdebug(f'# timeout at {session_timeout}')

        self.sessions_add(usr_id,
                          latest_reserve_time=latest_reserve_time,
                          arrival_time=arrival_time,
                          session_timeout=session_timeout,
                          reserved=False,
                          reservation={})

        resp.its_id = its_id
        resp.transit_time = self.TRANSIT_TIME
        resp.latest_reserve_time = latest_reserve_time.isoformat()

        rospy.logdebug(f'# session added, {resp.its_id=}')

    @service_wrp(Notify, method=True)
    def notify_srv(self, req, resp):
        arrival_time = datetime.fromisoformat(req.arrival_time)

        latest_reserve_time = self.latest_reserve_time(req.usr_id, arrival_time)
        now = datetime.now()

        if latest_reserve_time < now:
            rospy.loginfo(f'Invalid notification from {req.usr_id}: Earlier than HPV.')
            resp.transit_time = -1000
            return

        for sess in self.select_session(req.usr_id):
            if sess['reserved']: 
                resp.transit_time = -2000
                return # don't update
            else:
                sess['latest_reserve_time'] = latest_reserve_time
                sess['arrival_time'] = arrival_time
                sess['session_timeout'] = arrival_time + self.SESSION_TIMEOUT
                break # update ok
        else:
            rospy.loginfo(f'Invalid notification from {req.usr_id}: Unconnected user.')
            resp.transit_time = -3000
            return # don't update

        resp.latest_reserve_time = latest_reserve_time.isoformat()
        resp.transit_time = self.TRANSIT_TIME
        return

    def latest_reserve_time(self, ego_sid, ego_arrival_time):
        num_unreserved_hpv = 0 # count how many high-priority vehicles are unreserved
        for oth_sid, oth_sess in self.get_sessions:
            # skip if other = ego
            if oth_sid == ego_sid: continue
            
            ego_before_other = (oth_sess['arrival_time'] - ego_arrival_time).total_seconds()
            if oth_sess['reserved']:
                # err if other is reserved and ego is earlier than other
                if 0 < ego_before_other: return datetime.now()
                # skip if other is reserved
                else: continue 
            else:
                # skip if other is not reserved and ego is earlier than other
                if 0 < ego_before_other: continue
                # skip if other is reserved and other is far ahead of ego
                elif self.TIME_HORIZON < -ego_before_other: continue
                # count if other is not reserved and other is earlier than ego
                else:
                    num_unreserved_hpv += 1
                    continue

            assert False, 'Unreachable code'

        time_needed = self.COMPUTE_TIME * (num_unreserved_hpv+1)
        time_needed += self.COMPUTE_TIME # heuristic extra time
        return ego_arrival_time - timedelta(seconds=time_needed)

    def state_cb(self, state_msg):
        now = datetime.now() # .replace(microsecond=0)
        time_log = []

        ## BLOCK1

        usr_id = state_msg.child_frame_id
        x = state_msg.x
        y = state_msg.y
        h = state_msg.yaw
        v = state_msg.v

        if usr_id not in self.sessions:
            return # not connected yet
        
        time_log.append(datetime.now() - now) 
        now += time_log[-1]

        ## BLOCK 2

        for sess in self.select_session(usr_id):
            if not sess['reserved']: return # not reserved so no limits avail
            time_ref = sess['reservation']['time_ref']
            pass4 = sess['reservation']['analysis']['pass4']    
        
        time_log.append(datetime.now() - now) 
        now += time_log[-1]

        ## BLOCK 3

        # lookahead = np.array([x + 0.3*np.cos(h), y + 0.3*np.sin(h)])
        # i = ceil((now - time_ref).total_seconds() // self.TIME_STEP)
        # i = min(i + 5, len(self.solver.timeline) - 1)
        state = np.array([x, y, h, 0, v])
        i = (now - time_ref).total_seconds() // self.TIME_STEP

        if not 0 <= i < len(self.solver.timeline)-1:
            rospy.loginfo('State outside of timeline for Limits: %s', usr_id)
            return # outside timeline

        time_log.append(datetime.now() - now) 
        now += time_log[-1]

        ## BLOCK 4

        # idx = (lookahead - self.grid.domain.lo[:2]) / np.array(self.grid.spacings[:2])
        # idx = np.where(self.grid._is_periodic_dim[:2], idx % np.array(self.grid.shape[:2]), idx)
        # idx = np.round(idx).astype(int)
        idx = (np.array([x, y, h]) - self.grid.domain.lo[:3]) / np.array(self.grid.spacings)[:3]
        idx = np.where(self.grid._is_periodic_dim[:3], idx % np.array(self.grid.shape[:3]), idx)
        idx = np.round(idx).astype(int)

        time_log.append(datetime.now() - now) 
        now += time_log[-1]

        ## BLOCK 5

        def lrcs(vf, x, i):
            now = datetime.now()
            time_log = []

            ## BLOCK 5.1

            f = np.array([
                x[4] * np.cos(x[2]),
                x[4] * np.sin(x[2]),
                (x[4] * np.tan(x[3]))/self.solver.reach_dynamics.wheelbase,
                0.,
                0.,
            ])
            g = np.array([
                [0., 0.],
                [0., 0.],
                [0., 0.],
                [1., 0.],
                [0., 1.],
            ])

            time_log.append(datetime.now() - now)
            now += time_log[-1]

            ## BLOCK 5.2

            ix = self.solver.nearest_index(x)
            dvdx = self.solver.spatial_deriv(vf[i], ix)

            time_log.append(datetime.now() - now) 
            now += time_log[-1]

            ## BLOCK 5.3

            a = np.array(vf[(i+1, *ix)] + self.solver.time_step*(dvdx.T @ f))
            b = np.array(self.solver.time_step*(dvdx.T @ g))

            time_log.append(datetime.now() - now) 
            now += time_log[-1]

            ## BLOCK 5.4

            control_space = np.array([
                self.solver.reach_dynamics.control_space.lo,
                self.solver.reach_dynamics.control_space.hi,
            ]).T
            control_vecs = [np.linspace(*lohi) for lohi in control_space]
            control_grid = np.meshgrid(*control_vecs)

            time_log.append(datetime.now() - now) 
            now += time_log[-1]

            ## BLOCK 5.6

            # This is the important part.
            # Essentially we want: a + b \cdot u <= 0
            # Here, `mask` masks the set over the control space spanned by `control_vecs`
            terms = [us*b_ for us, b_ in zip(control_grid, b)]
            mask = sum(terms, a) <= 0

            time_log.append(datetime.now() - now) 
            now += time_log[-1]

            ## BLOCK 5.7

            save_path = Path(f'/svea_ws/src/ltms/data/{usr_id}/lrcs_{i}.npy')
            if not save_path.exists():
                save_path.mkdir(exist_ok=True)
                np.save(save_path / f'ltms_{i}.npy', mask, allow_pickle=True)

            rospy.logdebug('\n'.join(['Took:'] + [
                f'  Block 5.{i+1}: {dt.total_seconds()}'
                for i, dt in enumerate(time_log)
            ]))

            return mask, control_vecs

        # vf = shp.project_onto(pass4, 1, 2)
        # jdx, dist = closest_subzero(vf, idx)
        # rospy.loginfo(f'{dist=}')
        # state = np.array([
        #     self.solver.grid.coordinate_vectors[n][j]
        #     for n, j in enumerate(jdx)
        # ])
        state = np.array([x, y, h, 0, v])
        mask, ctrl_vecs = lrcs(pass4, state, ceil(i))

        time_log.append(datetime.now() - now) 
        now += time_log[-1]

        ## BLOCK 6
        limits_msg = NamedBytes(usr_id, state_msg.header.stamp, [x - 128 for x in mask.tobytes()])
        self.Limits.publish(limits_msg)

        time_log.append(datetime.now() - now) 
        now += time_log[-1]

        ## BLOCK 7

        rospy.logdebug('\n'.join(['Took:'] + [
            f'  Block {i+1}: {dt.total_seconds()}'
            for i, dt in enumerate(time_log)
        ] + [
            f'  Total: {sum([dt.total_seconds() for dt in time_log])}'
        ]))
        rospy.loginfo('Sending Limits')

    @service_wrp(Reserve, method=True)
    def reserve_srv(self, req, resp):
        resp.success = False
        resp.reason = 'Unknown.'

        try:
            time_ref = datetime.fromisoformat(req.time_ref)
        except Exception:
            resp.reason = f"Malformed ISO time: '{req.time_ref}'."
            return

        if req.name not in self.sessions:
            rospy.loginfo(f"Reservation cannot be done for unknown session: '{req.name}'.")

        if req.entry not in self.ENTRY_LOCATIONS:
            resp.reason = f"Illegal entry region: '{req.entry}'."
            return
        if req.exit not in self.EXIT_LOCATIONS:
            resp.reason = f"Illegal exit region: '{req.exit}'."
            return
        if (req.entry, req.exit) not in self.PERMITTED_ROUTES:
            resp.reason = f"Illegal route through region: '{req.entry}' -> '{req.exit}'."
            return
        
        latest_reserve_time = self.latest_reserve_time(req.name, time_ref)
        now = datetime.now()
        if latest_reserve_time < now:
            resp.reason = f'Reservation late: {(now - latest_reserve_time).total_seconds()} s too late'
            rospy.logdebug(f'# {resp.reason}')
            return

        rospy.loginfo(f"Reservation request from '{req.name}': {req.entry} -> {req.exit}")

        self.reserve(req.name, req, resp)

        if not resp.success:
            rospy.loginfo(f"Reservation for {req.name} recjected: {resp.reason}")
            return

        rospy.loginfo(f"Reservation for {req.name} approved.")

    def reserve(self, sid, req, resp):

        try:
            time_ref = datetime.fromisoformat(req.time_ref)
        except Exception:
            resp.reason = f"Malformed ISO time: '{req.time_ref}'."
            return
        
        # Debugging path
        save_path = None # to disable
        save_path = Path(f'/svea_ws/src/ltms/data/{sid}')
        save_path.mkdir(exist_ok=True)

        result = {}
        try:
            earliest_entry = round(max(req.earliest_entry, 0), 1)
            latest_entry = round(min(req.latest_entry, self.TIME_HORIZON), 1)
            
            offset = round(floor(earliest_entry) + (earliest_entry % self.TIME_STEP), 1)
            time_ref += timedelta(seconds=offset)
            earliest_entry -= offset
            latest_entry -= offset
            assert 0 <= earliest_entry <= latest_entry <= self.TIME_HORIZON, \
                f'Negotiation Failed: Invalid window offsetting (offset={offset})'

            max_window_entry = round(min(latest_entry - earliest_entry, self.MAX_WINDOW_ENTRY), 1)
            assert max_window_entry, 'Negotiation Failed: Invalid entry window requested'
            
            dangers = self.resolve_dangers(time_ref)
            dangers = [danger
                       for _id, danger in dangers.items()
                       if _id[:5] != sid[:5]]

            self.solver.run_analysis('pass2', 'pass3', 'pass4',
                                     min_window_entry=1,  max_window_entry=max_window_entry,
                                     min_window_exit=1, max_window_exit=2.0,
                                     pass1=self.offline_passes[req.entry, req.exit],
                                     entry=self.environment[req.entry],
                                     exit=self.environment[req.exit],
                                     dangers=dangers,
                                     result=result,
                                     save_path=save_path,
                                     interactive=False)

            earliest_entry = max(earliest_entry, result['earliest_entry'])
            latest_entry = min(latest_entry, result['latest_entry'])
            earliest_exit = result['earliest_exit']
            latest_exit = result['latest_exit']
            assert 0 < latest_entry - earliest_entry, 'Negotiation Faild: No time window to enter region'

        except AssertionError as e:
            msg, = e.args
            resp.reason = f'Reservation Error: {msg}'

        else:
            self.add_reservation(sid,
                                 name=req.name, 
                                 time_ref=time_ref,
                                 entry=req.entry, exit=req.exit,
                                 earliest_entry=earliest_entry,
                                 latest_entry=latest_entry,
                                 earliest_exit=earliest_exit,
                                 latest_exit=latest_exit,
                                 analysis=result)
            
            # flat_corridor = shp.project_onto(result['pass4'], 1, 2) <= 0

            resp.time_ref = time_ref.isoformat()
            resp.earliest_entry = earliest_entry
            resp.latest_entry = latest_entry
            resp.earliest_exit = earliest_exit
            resp.latest_exit = latest_exit
            # resp.shape = list(flat_corridor.shape)
            # resp.corridor = flat_corridor.tobytes()
            resp.success = True
            resp.reason = ''

        # with open(f'/svea_ws/src/ltms/data/{sid}.json', 'w') as f:
        #     json.dump({
        #         'time_ref': time_ref.isoformat(),
        #         'earliest_entry': earliest_entry,
        #         'latest_entry': latest_entry,
        #         'earliest_exit': earliest_exit,
        #         'latest_exit': latest_exit,
        #         'output_earliest_entry': result['earliest_entry'],
        #         'output_latest_entry': result['latest_entry'],
        #         'output_earliest_exit': result['earliest_exit'],
        #         'output_latest_exit': result['latest_exit'],
        #     }, f)
        
    def resolve_dangers(self, time_ref, quiet=False):

        td_horizon = timedelta(seconds=self.TIME_HORIZON)
        
        dangers = {}
        for sid, reservation in self.get_reservations:
            earliest_overlap = max(time_ref, reservation['time_ref'])
            latest_overlap = min(time_ref + td_horizon, reservation['time_ref'] + td_horizon)
            overlap = (latest_overlap - earliest_overlap).total_seconds()

            if not 0 < overlap:
                continue
            
            danger = np.ones(self.solver.timeline.shape + self.grid.shape)
            if time_ref < earliest_overlap:
                # HPV:     [-----j----)
                # LPV: [---i-----)
                i_offset = (earliest_overlap - time_ref).total_seconds()
                j_offset = (latest_overlap - reservation['time_ref']).total_seconds()
                i = ceil(i_offset / self.TIME_STEP)
                j = ceil(j_offset / self.TIME_STEP)
                danger[i:] = reservation['analysis']['pass4'][:j]
            else: 
                # HPV: [---i-----)
                # LPV:     [-----j----)
                i_offset = (earliest_overlap - reservation['time_ref']).total_seconds()
                j_offset = (latest_overlap - time_ref).total_seconds()
                i = ceil(i_offset / self.TIME_STEP)
                j = ceil(j_offset / self.TIME_STEP)
                danger[:j] = reservation['analysis']['pass4'][i:i+j]
            dangers[sid] = danger

        if not dangers and not quiet:
            rospy.loginfo('Intersection free!')

        return dangers

    def tube_to_marker(self, tube):
        pass

    def run(self):
        
        rate = rospy.Rate(5)

        while not rospy.is_shutdown():
            now = datetime.now()

            for tube in self.resolve_dangers(now, quiet=True):
                marker = self.tube_to_marker(tube)

            rate.sleep()

if __name__ == '__main__':

    ## Start node ##
    Server().run()

