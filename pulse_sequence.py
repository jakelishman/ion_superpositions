"""
Supplementary types:
state_specifier: (motional * ?internal * ?phase) --
    motional: unsigned int --
        The motional level of the state, e.g. 0, 2, 3 etc.
    internal (optional, 'g'): 'g' | 'e' --
        The internal part of the state, either ground or excited.  Defaults to
        ground.
    phase (optional, 0.0): double --
        The relative phase of self state as an angle, divided by pi.  The
        resulting phase will be `e^(i * pi * phase)`, for example:
            0.0 => 1.0 + 0.0i,
            0.5 => 0.0 + 1.0i,
            1.0 => -1.0 + 0.0i,
            3.0 => -1.0 + 0.0i.

    Access self type using functions from state_specifier.py.
"""

from cmath import exp as cexp
from itertools import chain
from functools import reduce
from random import SystemRandom
import math
import ion_superpositions.state_specifier as state
import ion_superpositions.pulse_matrices as pm
import numpy as np
import scipy.optimize

__all__ = ['PulseSequence']

def _random_array(shape, lower=0.0, upper=1.0, **kwargs):
    """random_array(shape, lower=0.0, upper=1.0, **kwargs) -> array

    Return a np.array of `shape` with the elements filled with uniform random
    values between `lower` and `upper`."""
    rand = SystemRandom()
    length = reduce(lambda acc, x: acc * x, shape, 1)\
             if isinstance(shape, tuple) else shape
    rands = [rand.uniform(lower, upper) for _ in range(length)]
    return np.array(rands, **kwargs).reshape(shape)

def _format_complex(z, n_digits):
    z = round(z.real, n_digits) + round(z.imag, n_digits) * 1.0j
    if z.imag == 0:
        return str(z.real)
    elif z.real == 0:
        return "{}i".format(z.imag)
    return "({0} {2} {1}i)".format(z.real, abs(z.imag),
                                   {-1: '-', 1: '+'}[np.sign(z.imag)])

class PulseSequence(object):
    """
    Arguments:
    colours: 1D list of ('c' | 'r' | 'b') --
        The sequence of coloured pulses to apply, with 'c' being the carrier,
        'r' being the first red sideband and 'b' being the first blue sideband.
        If the array looks like
            ['r', 'b', 'c'],
        then the carrier will be applied first, then the blue, then the red,
        similar to how we'd write them in operator notation.

    target (optional, None): 1D list of state_specifier --
        The states which should be populated with equal probabilities after the
        pulse sequences is completed.  The relative phases are allowed to vary
        if `fixed_phase` is False.  For example, `[0, (2, 'e', 0.5), 3]` and
        `fixed_phase == False` corresponds to a target state
        `(|g0> + e^{ia}|e2> + e^{ib}|g3>)/sqrt(3)` with variable `a` and `b`
        (i.e. the choice of phase of the |e2> state is ignored).

        If not set, all functions which require a target will throw
        `AssertionError`.

    fixed_phase (optional, False): Boolean --
        Whether the phases of the target should be fixed during fidelity,
        distance and optimisation calculations.

    start (optional, [0]): 1D list of state_specifier --
        The motional levels that should be populated in the ground state at the
        beginning of the pulse sequence.  These will be equally populated, for
        example, `[0, (2, 'e', 0.5), 3]` corresponds to a start state of
        `(|g0> + i|e2> + |g3>)/sqrt(3)`.
    """
    def __init__(self, colours, target=None, fixed_phase=False, start=None):
        assert len(colours) > 0,\
            "You must have at least one colour in the sequence!"
        start = start if start is not None else [(0, 'g', 0.0)]
        self.colours = colours
        self.__len = len(self.colours)
        self.__n_phases = len(target) - 1 if target is not None else None
        self.ns = max(pm.motional_states_needed(colours),
                        max(list(map(state.motional, start))) + 1,
                        max(list(map(state.motional, target))) + 1\
                        if target is not None else 0)
        self.fixed_phase = fixed_phase or self.__n_phases is 0
        self.target = target
        self.__target = pm.build_state_vector(target, self.ns)\
                        if target is not None else None
        self.start = pm.build_state_vector(start, self.ns)
        self.__lin_ops =\
            np.array([pm.ColourOperator(c, self.ns) for c in self.colours])
        # The angles are for every colour in the sequence, read in left-to-right
        # order.  The angles are all divided by pi.
        self.__angles = None
        # The stored phases are for every element of the target except the
        # first.  The first is always taken to be 0, and the others are rotated
        # so that self is true (if a first phase is supplied).  The phases are
        # stored as angles, divided by pi (e.g. 0 => 1, 0.5 => i, 1 => -1 etc).
        self.__phases = None

        # Output storage.
        self.__u = np.empty((2 * self.ns, 2 * self.ns), dtype=np.complex128)
        self.__d_u = np.empty((self.__len, 2 * self.ns, 2 * self.ns),
                              dtype=np.complex128)
        if target is not None:
            # __new_target shouldn't be a np.array because it needs to be able
            # to hold state_specifier tuples which are variable length.
            self.__new_target = [0 for _ in self.target]
            self.__dist = float("inf")
            self.__d_dist_angles = np.empty(self.__len, dtype=np.float64)
            self.__d_dist_phases = np.empty(len(target) - 1, dtype=np.float64)

        # Pre-allocate calculation scratch space and fixed variables.
        self.__id = np.identity(2 * self.ns, dtype=np.complex128)
        self.__partials_ltr = np.empty_like(self.__d_u)
        self.__partials_rtl = np.empty_like(self.__d_u)
        self.__partials_ltr[0] = self.__id
        self.__partials_rtl[0] = self.__id
        self.__temp = np.empty_like(self.__u)
        self.__tus = 0.0j

    def __update_target_phases(self):
        self.__new_target[0] = state.set_phase(self.target[0], 0)
        for i, el in enumerate(self.target[1:]):
            self.__new_target[i + 1] = state.set_phase(el, self.__phases[i])
        self.__target = pm.build_state_vector(self.__new_target, self.ns)

    def __update_propagator_and_derivatives(self):
        """
        Efficient method of calculating the complete propagator, and all the
        derivatives associated with it.

        Arguments:
        pulses: 1D list of (colour * angle) --
            colour: 'c' | 'r' | 'b' --
                The colour of the pulse to be applied, 'c' is the carrier, 'r' is
                the first red sideband and 'b' is the first blue sideband.

            angle: double --
                The angle of specified pulse divided by pi, e.g. `angle = 0.5`
                corresponds to the pulse being applied for an angle of `pi / 2`.

            A list of the pulses to apply, at the given colour and angle.

        ns: unsigned --
            The number of motional states to consider when building the matrices.
            Note self is not the maximum motional state - the max will be |ns - 1>.

        Returns:
        propagator: 2D complex numpy.array --
            The full propagator for the chain of operators, identical to calling
            `multi_propagator(colours)(angles)`.

        derivatives: 1D list of (2D complex numpy.array) --
            A list of the derivatives of the propagator at the specified angles,
            with respect to each of the given angles in turn.
        """
        for i in range(self.__len - 1):
            np.dot(self.__partials_ltr[i], self.__lin_ops[i].op,
                   out=self.__partials_ltr[i + 1])
            np.dot(self.__lin_ops[-(i + 1)].op, self.__partials_rtl[i],
                   out=self.__partials_rtl[i + 1])

        np.dot(self.__partials_ltr[-1], self.__lin_ops[-1].op, out=self.__u)
        for i in range(self.__len):
            np.dot(self.__partials_ltr[i], self.__lin_ops[i].d_op,
                   out=self.__temp)
            np.dot(self.__temp, self.__partials_rtl[-(i + 1)], out=self.__d_u[i])

    def __update_distance(self):
        self.__tus = pm.inner_product(self.__target, self.__u, self.start)
        self.__dist = 1.0 - (self.__tus * np.conj(self.__tus)).real

    def __update_distance_angle_derivatives(self):
        for i in range(self.__len):
            prod = pm.inner_product(self.__target, self.__d_u[i], self.start)
            self.__d_dist_angles[i] = -2.0 * (np.conj(self.__tus) * prod).real

    def __update_distance_phase_derivatives(self):
        pref = 2.0 / math.sqrt(len(self.target))
        u_start = np.dot(self.__u, self.start)
        for i, pre_phase in enumerate(self.__phases):
            phase = cexp(1.0j * math.pi * (0.5 - pre_phase))
            # we can calculate the inner product <g n_j|U|start> by
            # precalculating U|start>, then indexing to the relevant element.
            idx = state.idx(self.target[i + 1], self.ns)
            self.__d_dist_phases[i] =\
                pref * (phase * u_start[idx] * np.conj(self.__tus)).real

    def __update_angles_if_required(self, angles):
        if angles is None:
            return False
        assert len(angles) == self.__len,\
            "There are {} colours in the sequence, but I got {} angles."\
            .format(self.__len, len(angles))
        if np.array_equal(self.__angles, angles):
            return False
        self.__angles = angles
        for i in range(len(self.__angles)):
            self.__lin_ops[i].angle = self.__angles[i]
        return True

    def __update_phases_if_required(self, phases):
        if self.fixed_phase\
           or phases is None\
           or np.array_equal(self.__phases, phases):
            return False
        assert 0 <= len(self.target) - len(phases) <= 1,\
            "There are {} elements of the target, but I got {} phases."\
            .format(len(self.target), len(phases))
        if len(phases) == len(self.target) - 1:
            self.__phases = phases
        else:
            self.__phases = np.vectorize(lambda x: x - phases[0])(phases[1:])
        self.__update_target_phases()
        return True

    def __calculate_propagator(self, angles):
        if self.__update_angles_if_required(angles):
            self.__update_propagator_and_derivatives()

    def __calculate_all(self, angles, phases=None):
        assert self.__target is None or self.fixed_phase or phases is not None,\
            "If you're not in fixed phase mode, you need to specify the phases."
        updated_angles = self.__update_angles_if_required(angles)
        updated_phases = self.__update_phases_if_required(phases)
        if not (updated_angles or updated_phases):
            return
        if updated_angles:
            self.__update_propagator_and_derivatives()
        if self.__target is not None:
            self.__update_distance()
            if updated_angles:
                self.__update_distance_angle_derivatives()
            if updated_phases:
                self.__update_distance_phase_derivatives()

    def U(self, angles):
        """
        Get the propagator of the pulse sequence stored in the class with the
        specified angles.
        """
        self.__calculate_propagator(angles)
        return np.copy(self.__u)

    def d_U(self, angles):
        """
        Get the derivatives of the propagator of the pulse sequence stored in
        the class with the specified angles.
        """
        self.__calculate_propagator(angles)
        return np.copy(self.__d_u)

    def distance(self, angles, phases=None):
        """
        Get the distance of the pulse sequence stored in the class with the
        specified angles.
        """
        assert self.__target is not None,\
            "You must set the target state to calculate the distance."
        self.__calculate_all(angles, phases)
        return self.__dist

    def d_distance(self, angles, phases=None):
        """
        Get the derivatives of the distance of the pulse sequence stored in the
        class with the specified angles (and phases of the target state, if
        applicable).

        Outputs (angles * phases), with each being a 1D np.array.  The pulse
        angles are in left-to-right order (i.e. for "rcb", the order is
        [r, c, b]), and the phases are in the order that the elements of the
        target were given, excluding the first element of the target, which is
        assumed to maintain a phase of 1.
        """
        assert self.__target is not None,\
            "You must set the target state to calculate the distance."
        assert self.fixed_phase or phases is not None,\
            "If you're not in fixed phase mode, you need to specify the phases."
        self.__calculate_all(angles, phases)
        if self.fixed_phase:
            return np.copy(self.__d_dist_angles)
        return np.copy(self.__d_dist_angles), np.copy(self.__d_dist_phases)

    def optimise(self, initial_angles=None, initial_phases=None, **kwargs):
        """optimise(initial_angles=None, initial_phases=None, **kwargs)"""
        assert self.__target is not None,\
            "You must set the target state to optimise a pulse sequence."
        angles = _random_array(self.__len, dtype=np.float64)\
                 if initial_angles is None else initial_angles
        if not self.fixed_phase:
            phases = _random_array(self.__n_phases, dtype=np.float64)\
                     if initial_phases is None else initial_phases
            assert len(phases) == self.__n_phases
            def _split(f):
                return lambda x: f(x[:-self.__n_phases], x[-self.__n_phases:])
            target_f = _split(self.distance)
            jacobian = lambda xs: np.concatenate(_split(self.d_distance)(xs))
            inits = np.concatenate((angles, phases))
        else:
            target_f = self.distance
            jacobian = self.d_distance
            inits = angles
        return scipy.optimize.minimize(target_f, inits, jac=jacobian,
                                       method='BFGS', **kwargs)

    def split_result(self, opt_res):
        """split_result(opt_res) -> angles, phases"""
        if self.fixed_phase:
            return opt_res.x, np.zeros(1, dtype=np.float64)
        return opt_res.x[:-self.__n_phases],\
               np.insert(opt_res.x[-self.__n_phases:], 0, 0.0)

    def __print_trace(self, trace_out, n_digits=5):
        _transpose = lambda x: map(list, np.transpose(list(map(list, x))))
        _reorder_ind = lambda x: map(reversed, _transpose(map(reversed, x)))
        _colour_str = lambda cop: "{}({})".format(cop.colour,
                                                  round(cop.angle, n_digits))
        def _normalise_string_lengths(lst):
            maxl = reduce(lambda acc, s: max(acc, len(s)), lst, 0)
            return [s + " " * (maxl - len(s)) for s in lst]
        def _split_ground_excited(arr):
            split = lambda lst: [lst[:len(lst) // 2], lst[len(lst) // 2:]]
            return list(chain.from_iterable(map(split, arr)))
        def _group_pulses(arr):
            pair = lambda line: ["  ".join(line[2 * i : 2 * i + 2])\
                                 for i in range(len(line) // 2)]
            return [pair(list(line)) for line in arr]

        str_cols = [[_format_complex(x, 5) for x in lst] for lst in trace_out]
        str_cols = _split_ground_excited(str_cols)
        for i, col in enumerate(str_cols):
            str_cols[i] = col + ["|e>" if i % 2 is 0 else "|g>"]
        str_cols = map(_normalise_string_lengths, str_cols)
        str_cols = _reorder_ind(str_cols)
        colour_strings = list(map(_colour_str, self.__lin_ops)) + ["start"]
        str_cols = _transpose([colour_strings] + _group_pulses(str_cols))
        motionals = ["|{}>".format(i) for i in range(self.ns - 1, -1, -1)]
        str_cols = [["", ""] + motionals] + list(str_cols)
        for line in _transpose(map(_normalise_string_lengths, str_cols)):
            print("  |  ".join(line))

    def trace(self, angles=None, fmt=True):
        """
        Prettily print the evolved state after each pulse of the colour
        sequence.  If `format == False`, then the states (including the start
        state) will be returned as a list, and nothing will be printed.

        If the angles of the pulses are not specified then the last used set of
        angles will be traced instead.  self is useful for tracing the immediate
        output of an optimise call.
        """
        self.__update_angles_if_required(angles)
        out = np.empty((self.__len + 1, 2 * self.ns), dtype=np.complex128)
        out[0] = self.start
        for i in range(self.__len):
            out[i + 1] = np.dot(self.__lin_ops[self.__len - i - 1].op, out[i])
        if not fmt:
            return out
        else:
            self.__print_trace(out)
