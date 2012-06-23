# atomic model


#TODO revisit import statements and reorganize
from scipy import interpolate
import line
import constants
import numpy as np
import sqlite3
import logging
import StringIO
import pkgutil

try:
    import sqlparse

    sqlparse_available = True
except ImportError:
    sqlparse_available = False

logger = logging.getLogger(__name__)


def read_atomic_data(fname=None):
    if fname is None:
        data = np.recfromtxt(StringIO.StringIO(
            pkgutil.get_data('tardis', 'data/atoms.dat')),
            names=('atom', 'symbol', 'mass'))
    else:
        data = np.recfromtxt(fname,
            names=('atom', 'symbol', 'mass'))
    return data


def read_ionization_data(fname=None):
    if fname is None:
        data = np.recfromtxt(StringIO.StringIO(
            pkgutil.get_data('tardis', 'data/ionization.dat')),
            names=('atom', 'ion', 'energy'))
    else:
        data = np.recfromtxt(fname,
            names=('atom', 'ion', 'energy'))
    return data


def convert_int_ndarray(sqlite_binary):
    if sqlite_binary == '-1':
        return np.array([], dtype=np.int64)
    else:
        return np.frombuffer(sqlite_binary, dtype=np.int64)


def convert_float_ndarray(sqlite_binary):
    if sqlite_binary == '-1.0':
        return np.array([], dtype=np.float64)
    else:
        return np.frombuffer(sqlite_binary, dtype=np.float64)

sqlite3.register_converter('int_ndarray', convert_int_ndarray)
sqlite3.register_converter('float_ndarray', convert_float_ndarray)


class AtomModel(object):
    pass


class KuruczAtomModel(AtomModel):
    @classmethod
    def from_db(cls, conn, max_atom=30, max_ion=30):
        logger.info('Reading Kurucz model from database max atom=%d max ion=%d', max_atom, max_ion)
        masses = read_atomic_data()['mass'][:max_atom]

        ionization_data = read_ionization_data()


        #reason for max_ion - 1: in energy level data there's unionized, once-ionized, twice-ionized, ...
        #in ionization_energies, there's only once_ionized, twice_ionized
        ionization_energy = np.zeros((max_ion - 1, max_atom))

        for atom, ion, ion_energy in ionization_data:
            if atom > max_atom or ion >= max_ion:
                continue
            ionization_energy[ion - 1, atom - 1] = ion_energy

        levels_energy, levels_g, levels_metastable = read_kurucz_level_data_fromdb(conn, max_atom, max_ion)

        return cls(masses=masses,
            ionization_energy=ionization_energy,
            levels_energy=levels_energy,
            levels_g=levels_g,
            levels_metastable=levels_metastable,
            max_atom=max_atom,
            max_ion=max_ion)

    def __init__(self,
                 masses=None,
                 ionization_energy=None,
                 levels_energy=None,
                 levels_g=None,
                 levels_metastable=None,
                 max_atom=None,
                 max_ion=None):
        self.masses = masses
        self.ionization_energy = ionization_energy
        self.levels_energy = levels_energy
        self.levels_g = levels_g

        self.levels_metastable = levels_metastable
        self.max_atom = max_atom
        self.max_ion = max_ion

    def calculate_radfield_correction_factor(self, t_rad, t_electron, w, departure_coefficient=None,
                                             xi_threshold_species=(1, 19)):
    #factor delta ML 1993
        if departure_coefficient is None:
            departure_coefficient = 1 / float(w)
        delta = np.ones((self.max_ion - 1, self.max_atom))
        xi_threshold = self.ionization_energy[xi_threshold_species]

        #Formula 15 ML 1993
        threshold_filter = (self.ionization_energy <= xi_threshold) & (self.ionization_energy > 0)
        delta[threshold_filter] = (t_electron / (departure_coefficient * w * t_rad)) *\
                                  np.exp((delta[threshold_filter] / (constants.kbinev * t_rad))\
                                  - (delta[threshold_filter] / (constants.kbinev * t_electron)))

        threshold_filter = (self.ionization_energy > xi_threshold) & (self.ionization_energy > 0)
        #Formula 20 ML 1993
        delta[self.ionization_energy > xi_threshold] = 1 -\
                                                       np.exp((delta[threshold_filter] / (constants.kbinev * t_rad))\
                                                       - (xi_threshold / (constants.kbinev * t_rad))) +\
                                                       (t_electron / (departure_coefficient * w * t_rad)) *\
                                                       np.exp((delta[threshold_filter] / (constants.kbinev * t_rad))\
                                                       - (delta[threshold_filter] / (constants.kbinev * t_electron)))

        return delta


class KuruczMacroAtomModel(KuruczAtomModel):
    @classmethod
    def from_db(cls, conn, max_atom=30, max_ion=30):
        kurucz_atom_model = KuruczAtomModel.from_db(conn, max_atom=max_atom, max_ion=max_ion)
        kurucz_atom_model.macro_atom = line.SimpleMacroAtomData.fromdb(conn)
        return kurucz_atom_model


class CombinedAtomicModel(KuruczAtomModel):
    """Complex Atomic Model, in addition to reading the Kurucz model.
        In addition it reads the recombination to ground state coefficient zeta
        from the zeta table.
        It also includes reading reading the meta stable levels and transition probabilities

        """

    @classmethod
    def from_db(cls, conn, max_atom=30, max_ion=30):
        logger.info('Reading Kurucz model from database max atom=%d max ion=%d', max_atom, max_ion)

        combined_atomic_model = KuruczAtomModel.from_db(conn, max_atom=max_atom, max_ion=max_ion)
        combined_atomic_model.line_list = line.read_line_list(conn, max_atom=max_atom, max_ion=max_ion)
        combined_atomic_model.macro_atom = line.SimpleMacroAtomData.fromdb(conn)

        #factor zeta ML 1993
        combined_atomic_model.recombination_coeffiencents_t_rads, combined_atomic_model.recombination_coeffiencents =\
            read_recombination_coefficients_fromdb(conn, max_atom, max_ion)
        #combined_atomic_model.interpolate_recombination_coefficient =\
        #read_recombination_coefficients_fromdb(conn, max_atom, max_ion)

        return combined_atomic_model

    def interpolate_recombination_coefficient(self, t_rad, kind='linear', bounds_error=True, fill_value=1.):
        interpolator = interpolate.interp1d(self.recombination_coeffiencents_t_rads,
                                            self.recombination_coeffiencents,
                                            kind=kind,
                                            bounds_error=bounds_error,
                                            fill_value=fill_value)
        return interpolator(t_rad)

def read_recombination_coefficients_fromdb(conn, max_atom=30, max_ion=30):
    select_zeta_stmt = """select
                            atom, ion, zeta
                        from
                            zeta
                        where
                                atom < %d
                            and
                                ion < %d
                        """ % (max_atom, max_ion)

    logger.debug('Reading recombination coefficients from db:\n%s\n%s\n%s',
        '-' * 80,
        select_zeta_stmt,
        '-' * 80)

    curs = conn.execute(select_zeta_stmt)
    t_rads = np.arange(2000, 42000, 2000)
    recombination_coefficients = np.ones((max_ion - 1, max_atom, len(t_rads)))
    for atom, ion, zeta in curs:
        recombination_coefficients[ion - 1, atom - 1] = zeta
    #interpolator = interpolate.interp1d(t_rads, recombination_coefficients, kind='linear', bounds_error=False,
    #    fill_value=1.)
    return t_rads, recombination_coefficients


def read_kurucz_level_data_fromdb(conn, max_atom=30, max_ion=None):
    #Constructing Matrix with atoms columns and ions rows
    #dtype is object and the cells will contain arrays with the energy levels
    if max_ion == None:
        max_ion = max_atom

    level_select_stmt = """select
                atom, ion, energy, g, metastable, level_id
            from
                levels
            where
                    atom <= %d
                and
                    ion < %d
            order by
                atom, ion, energy""" % (max_atom, max_ion)

    if sqlparse_available:
        logger.debug('Reading level data from db:\n%s\n%s\n%s',
            '-' * 80,
            sqlparse.format(level_select_stmt,
                reindent=True,
                keyword_case='upper'),

            '-' * 80)
    else:
        logger.debug(level_select_stmt, max_ion, max_atom)

    curs = conn.execute(level_select_stmt)
    energy_data = np.zeros((max_ion, max_atom), dtype='object')
    g_data = np.zeros((max_ion, max_atom), dtype='object')
    metastable_data = np.zeros((max_ion, max_atom), dtype='object')

    old_elem = None
    old_ion = None

    for elem, ion, energy, g, metastable, levelid in curs:
        if elem == old_elem and ion == old_ion:
            energy_data[ion, elem - 1] = np.append(energy_data[ion, elem - 1], energy)
            g_data[ion, elem - 1] = np.append(g_data[ion, elem - 1], g)
            metastable_data[ion, elem - 1] = np.append(metastable_data[ion, elem - 1], np.bool(metastable))
        else:
            old_elem = elem
            old_ion = ion
            energy_data[ion, elem - 1] = np.array([energy])
            g_data[ion, elem - 1] = np.array([g])
            metastable_data[ion, elem - 1] = np.array([np.bool(metastable)], dtype=np.bool)

    return energy_data, g_data, metastable_data