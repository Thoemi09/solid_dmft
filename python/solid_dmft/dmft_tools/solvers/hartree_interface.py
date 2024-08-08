import numpy as np
from itertools import product

from triqs.gf import MeshImTime, MeshReTime, MeshDLRImFreq, MeshReFreq, MeshLegendre, Gf, BlockGf, make_gf_imfreq, make_hermitian, Omega, iOmega_n, make_gf_from_fourier, make_gf_dlr, fit_gf_dlr, make_gf_dlr_imtime, make_gf_imtime
from triqs.gf.tools import inverse, make_zero_tail
from triqs.gf.descriptors import Fourier
from triqs.operators import c_dag, c, Operator, util
from triqs.operators.util.U_matrix import reduce_4index_to_2index
from triqs.operators.util.extractors import block_matrix_from_op
import triqs.utility.mpi as mpi
import itertools
from h5 import HDFArchive

from solid_dmft.io_tools.dict_to_h5 import prep_params_for_h5

from solid_dmft.dmft_tools import legendre_filter
from solid_dmft.dmft_tools.matheval import MathExpr


# import of the abstract class
from solid_dmft.dmft_tools.solvers.abstractdmftsolver import AbstractDMFTSolver

# import triqs solver
from triqs_hartree_fock import ImpuritySolver as hartree_solver
from triqs_hartree_fock.version import  triqs_hartree_fock_hash, version

class HartreeInterface(AbstractDMFTSolver):

    def __init__(self, general_params, solver_params, sum_k, icrsh, h_int, iteration_offset,
            deg_orbs_ftps, gw_params=None, advanced_params=None):

        # Call the base class constructor
        super().__init__(general_params, solver_params, sum_k, icrsh, h_int, iteration_offset,
            deg_orbs_ftps, gw_params, advanced_params)
        
        # Create the hartree solver specifics
    
        self.triqs_solver_params = {}
        keys_to_pass = ('method', 'one_shot', 'tol', 'with_fock')
        for key in keys_to_pass:
            self.triqs_solver_params[key] = self.solver_params[key]
        
        # sets up necessary GF objects on ImFreq
        self._init_ImFreq_objects()
        self._init_ReFreq_hartree() # definition at the end of the class



        # Construct the triqs_solver instances
        # Always initialize the solver with dc_U and dc_J equal to U and J and let the _interface_hartree_dc function
        # take care of changing the parameters
        gf_struct = self.sum_k.gf_struct_solver_list[self.icrsh]
        self.triqs_solver = hartree_solver(beta=self.general_params['beta'], gf_struct=gf_struct,
                                      n_iw=self.general_params['n_iw'], force_real=self.solver_params['force_real'],
                                      symmetries=[self._make_spin_equal],
                                      dc_U= self.general_params['U'][self.icrsh],
                                      dc_J= self.general_params['J'][self.icrsh]
                                      )
        
        # Give dc information to the solver in order to customize DC calculation
        def _interface_hartree_dc(hartree_instance, general_params, advanced_params, icrsh):
            """ Modifies in-place class attributes to infercace with options in solid_dmft
                for the moment supports only DC-relevant parameters

            Parameters
            ----------
                general_params : dict
                    solid_dmft general parameter dictionary
                advanced_params : dict
                    solid_dmft advanced parameter dictionary
                icrsh : int
                    correlated shell number
            """
            setattr(hartree_instance, 'dc', general_params['dc'])
            if general_params['dc_type'][icrsh] is not None:
                setattr(hartree_instance, 'dc_type', general_params['dc_type'][icrsh])

            for key in ['dc_factor', 'dc_fixed_value']:
                if key in advanced_params and advanced_params[key] is not None:
                    setattr(hartree_instance, key, advanced_params[key])

            #list valued keys
            for key in ['dc_U', 'dc_J', 'dc_fixed_occ']:
                if key in advanced_params and advanced_params[key][icrsh] is not None:
                    setattr(hartree_instance, key, advanced_params[key][icrsh])

            # Handle special cases
            if 'dc_dmft' in general_params:
                if general_params['dc_dmft'] == False:
                    mpi.report('HARTREE SOLVER: Warning dft occupation in the DC calculations are meaningless for the hartree solver, reverting to dmft occupations')

            if hartree_instance.dc_type == 0 and not self.general_params['magnetic']:
                    mpi.report(f"HARTREE SOLVER: Detected dc_type = {hartree_instance.dc_type}, changing to 'cFLL'")
                    hartree_instance.dc_type = 'cFLL'
            elif hartree_instance.dc_type == 0 and self.general_params['magnetic']:
                    mpi.report(f"HARTREE SOLVER: Detected dc_type = {hartree_instance.dc_type}, changing to 'sFLL'")
                    hartree_instance.dc_type = 'sFLL'
            elif hartree_instance.dc_type == 1:
                    mpi.report(f"HARTREE SOLVER: Detected dc_type = {hartree_instance.dc_type}, changing to 'cHeld'")
                    hartree_instance.dc_type = 'cHeld'
            elif hartree_instance.dc_type == 2 and not self.general_params['magnetic']:
                    mpi.report(f"HARTREE SOLVER: Detected dc_type = {hartree_instance.dc_type}, changing to 'cAMF'")
                    hartree_instance.dc_type = 'cAMF'
            elif hartree_instance.dc_type == 2 and self.general_params['magnetic']:
                    mpi.report(f"HARTREE SOLVER: Detected dc_type = {hartree_instance.dc_type}, changing to 'sAMF'")
                    hartree_instance.dc_type = 'sAMF'

        # Give dc information to the solver in order to customize DC calculation
        _interface_hartree_dc(self.triqs_solver, self.general_params, self.advanced_params, self.icrsh)


        # set up metadata
        self.git_hash = triqs_hartree_fock_hash
        self.version = version

        return
        
        
        
        
    def _init_ReFreq_hartree(self):
        r'''
        Initialize all ReFreq objects
        '''

        # create all ReFreq instances
        self.n_w = self.general_params['n_w']
        self.Sigma_Refreq = self.sum_k.block_structure.create_gf(ish=self.icrsh, gf_function=Gf, space='solver',
                                                                mesh=MeshReFreq(n_w=self.n_w, window=self.general_params['w_range'])
                                                                )
    
    def solve(self, **kwargs):

        # fill G0_freq from sum_k to solver
        self.triqs_solver.G0_iw << self.G0_freq

        # Solve the impurity problem for icrsh shell
        # *************************************
        # this is done on every node due to very slow bcast 
        self.triqs_solver.solve(h_int=self.h_int, **self.triqs_solver_params)

        # call postprocessing
        self.postprocess()

        return 

    def postprocess(self):
        r'''
        Organize G_freq, G_time, Sigma_freq and G_l from hartree solver
        '''

        # get everything from solver
        self.G0_freq << self.triqs_solver.G0_iw
        self.G_freq_unsym << self.triqs_solver.G_iw
        self.sum_k.symm_deg_gf(self.G_freq, ish=self.icrsh)
        self.G_freq << self.triqs_solver.G_iw
        for bl, gf in self.Sigma_freq:
            self.Sigma_freq[bl] << self.triqs_solver.Sigma_HF[bl]
            self.Sigma_Refreq[bl] << self.triqs_solver.Sigma_HF[bl]
        self.G_time << Fourier(self.G_freq)
        self.interaction_energy = self.triqs_solver.interaction_energy()
        self.DC_energy = self.triqs_solver.DC_energy()

        return




        
        
    