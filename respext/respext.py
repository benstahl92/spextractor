# ------------------------------------------------------------------------------
# respext --- redux of spextractor (https://github.com/astrobarn/spextractor)
#
#   an automated pEW, velocity, and absorption "depth" extractor
#   optimized for SN Ia spectra, though minimal support is 
#   available for SNe Ib and Ic (and could be further developed)
#
# this code base is a re-factorization and extension of the spextractor
# package written by Seméli Papadogiannakis (linked above)
# ------------------------------------------------------------------------------

# imports -- standard
import warnings
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import GPy
import pickle as pkl

# imports -- internal
from . import utils
from .lines import LINES, get_speed, pseudo_continuum, pEW, absorption_depth

class SpExtractor:
    '''container for a SN spectrum, with methods for all processing'''

    def __init__(self, spec_file, z, save_file = None, sn_type = 'Ia', flux_scale = 1, lambda_m_err = 'measure',
                 no_overlap = True, pEW_measure_from = 'data', pEW_err_method = 'default',
                 remove_gaps = True, auto_prune = True, sigma_outliers = None, downsampling = None, **kwargs):

        # store arguments from instantiation
        self.spec_file = spec_file
        self.z = z
        self.save_file = save_file
        self.sn_type = sn_type
        self.flux_scale = flux_scale
        self.lambda_m_err = lambda_m_err
        self.no_overlap = no_overlap
        self.pEW_measure_from = pEW_measure_from
        self.pEW_err_method = pEW_err_method

        # select appropriate set of spectral lines
        if self.sn_type not in ['Ia', 'Ia_LEGACY', 'Ib', 'Ic']:
            warnings.warn('{} is not a supported type, defaulting to Ia'.format(self.sn_type))
            self.sn_type = 'Ia'
        self.lines = LINES[self.sn_type].copy()

        # load and prepare spectrum
        self._prepare_spectrum(remove_gaps, auto_prune, sigma_outliers, downsampling, **kwargs)

        # instantiate DataFrame for continuum information
        # columns - 1/2: left/right continuum points, a: absorption minimum point, cont: continuum interpolator
        self.continuum = pd.DataFrame(columns = ['wav1', 'flux1', 'wava', 'fluxa', 'wav2', 'flux2', 'cont'],
                                      index = self.lines.index)

        # get model
        self._setup_model()
        if self.save_file is not None:
            self.load_model()
        else:
            self.save_file = self.spec_file + '.respext.sav'

    def save_model(self):
        '''save model'''
        with open(self.save_file, 'wb') as f:
            pkl.dump([self.model, self.mod_mean, self.mod_var, self.conf, self.mod_deriv], f)

    def load_model(self):
        '''load model from save file'''
        with open(self.save_file, 'rb') as f:
            tmp = pkl.load(f)
        self.model, self.mod_mean, self.mod_var, self.conf, self.mod_deriv = tmp

    def _prepare_spectrum(self, remove_gaps, auto_prune, sigma_outliers, downsampling, **kwargs):
        '''
        perform preparation steps of loading, de-redshifting, and normalizing flux of spectrum
        optional intermediate steps: remove gaps, prune, remove outliers, downsample
        '''

        wave, flux, flux_err, self.scale = utils.load_spectrum(self.spec_file, return_scale = True, **kwargs)
        wave = utils.de_redshift(wave, self.z)
        if remove_gaps:
            wave, flux, flux_err = utils.remove_gaps(wave, flux, flux_err)
        if auto_prune:
            wave, flux, flux_err = utils.auto_prune(wave, flux, flux_err, self.lines, **kwargs)
        if sigma_outliers is not None:
            wave, flux, flux_err = utils.filter_outliers(wave, flux, flux_err, sigma_outliers, **kwargs)
        if downsampling is not None:
            wave, flux, flux_err = utils.downsample(wave, flux, flux_err, downsampling)
        self.wave, self.flux, self.flux_err = wave, flux, flux_err

    def _setup_model(self):
        '''set up model'''

        self.x, self.y = self.wave[:, np.newaxis], self.flux[:, np.newaxis]
        kernel = GPy.kern.Matern32(input_dim = 1, lengthscale = 300, variance = 0.001)
        m = GPy.models.GPRegression(self.x, self.y, kernel)
        m['Gaussian.noise.variance'][0] = 0.0027
        self.model = m

    def _fit_model(self):
        '''fit model to data --- this step may take some time'''

        self.model.optimize()
        self.mod_mean, self.mod_var = self.model.predict(self.x)
        self.conf = np.sqrt(self.mod_var)
        self.mod_deriv = self.model.predictive_gradients(self.x)[0][:,0,0]

    def _get_continuum(self, feature):
        '''given a feature, automatically determine the continuum'''

        # run optimization if it has not already been done
        if not hasattr(self, 'mod_mean'):
            self._fit_model()

        # unpack feature information
        #rest_wavelength, low_1, high_1, low_2, high_2 = self.lines.loc[feature]

        # testing ---- enforce non-overlapping of continuum points
        if 'LEGACY' not in self.sn_type:
            rest_wavelength, low_1, high_1, low_2, high_2, blue_deriv, red_deriv = self.lines.loc[feature]
            if self.no_overlap:
                prev_iloc = self.continuum.index.get_loc(feature) - 1
                prev_blue_edge = self.continuum.loc[:, 'wav2'].iloc[prev_iloc]
                if (prev_iloc >= 0) and (prev_blue_edge > low_1) and (prev_blue_edge < high_1):
                     low_1 = self.continuum.loc[:, 'wav2'].iloc[prev_iloc]
        else:
            rest_wavelength, low_1, high_1, low_2, high_2 = self.lines.loc[feature]

        # identify indices of feature edge bounds
        cp_1 = np.searchsorted(self.x[:, 0], (low_1, high_1))
        index_low, index_hi = cp_1
        cp_2 = np.searchsorted(self.x[:, 0], (low_2, high_2))
        index_low_2, index_hi_2 = cp_2

        # check if feature outside range of spectrum
        if (index_low == index_hi) or (index_low_2 == index_hi_2):
            return False

        # identify indices of feature edges from where model peaks
        max_point = index_low + np.argmax(self.mod_mean[index_low: index_hi])
        max_point_2 = index_low_2 + np.argmax(self.mod_mean[index_low_2: index_hi_2])

        ### testing, find boundary by finding where derivative has passes through specified slope (usually zero) from + to - 
        if 'LEGACY' not in self.sn_type:
            # blue side
            pos = self.mod_deriv * (self.flux_scale * self.scale / self.flux.max()) - blue_deriv > 0 # where derivative is positive
            p_ind = (pos[:-1] & ~pos[1:]).nonzero()[0] # indices where last positive occurs before going negative
            max_point_cands = p_ind[(p_ind >= index_low) & (p_ind <= index_hi)]
            # red side
            pos = self.mod_deriv * (self.flux_scale * self.scale / self.flux.max()) - red_deriv > 0 # where derivative is positive
            p_ind = (pos[:-1] & ~pos[1:]).nonzero()[0] # indices where last positive occurs before going negative
            max_point_2_cands = p_ind[(p_ind >= index_low_2) & (p_ind <= index_hi_2)]
            # if at least one candidate for each, use those that have the highest maxima
            if (len(max_point_cands) >= 1) and (len(max_point_2_cands) >= 1):
                max_point = max_point_cands[np.argmax(self.mod_mean[max_point_cands])]
                max_point_2 = max_point_2_cands[np.argmax(self.mod_mean[max_point_2_cands])]
            else:
                return False

        # get wavelength, model flux at the feature edges and define continuum
        self.continuum.loc[feature, ['wav1', 'flux1']] = self.x[max_point, 0], self.mod_mean[max_point, 0]
        self.continuum.loc[feature, ['wav2', 'flux2']] = self.x[max_point_2, 0], self.mod_mean[max_point_2, 0]
        self.continuum.loc[feature, 'cont'] = pseudo_continuum(np.array([self.continuum.loc[feature, ['wav1', 'wav2']],
                                                               self.continuum.loc[feature, ['flux1', 'flux2']]]))

        return True

    def pick_continuum(self, features = None):
        '''interactively select continuum points'''

        if features is None:
            print('Select number(s) feature (or features separated by commas):')
            for idx, feature in enumerate(self.lines.index):
                print('  {}:  {}'.format(idx, feature))
            response = input('Selection > ')
            features = self.lines.index[[int(i) for i in response.split(',')]]
        elif type(features) == type('this is a string'):
            features = [features]
        # not checking that everything else is iterable, or that it has real features, so use correctly!

        for feature in features:
            selection = (self.wave > self.lines.loc[feature, 'low_1'] - 100) & (self.wave < self.lines.loc[feature, 'high_2'] + 100)
            self.continuum.loc[feature, ['wav1', 'flux1', 'wav2', 'flux2']] = utils.define_continuum(self.wave[selection],
                                                                                                     self.mod_mean[selection, 0],
                                                                                                     self.lines.loc[feature])
            self.continuum.loc[feature, 'cont'] = pseudo_continuum(np.array([self.continuum.loc[feature, ['wav1', 'wav2']],
                                                                   self.continuum.loc[feature, ['flux1', 'flux2']]]))

    def _get_feature_min(self, lambda_0, x_values, y_values, feature):
        '''compute location and flux of feature minimum'''

        # find deepest absorption
        min_pos = y_values.argmin()
        if (min_pos < 5) or (min_pos > y_values.shape[0] - 5):
            return np.nan, np.nan, np.nan, np.nan

        # measured wavelength and flux of feature
        lambda_m, flux_m = x_values[min_pos], y_values[min_pos]

        # sample possible spectra from posterior and find the minima
        samples = self.model.posterior_samples_f(x_values[:, np.newaxis], 100).squeeze().argmin(axis = 0)
        samples = samples[np.logical_and(samples != 0, samples != x_values.shape[0])]
        if samples.size == 0:
            return np.nan, np.nan, np.nan, np.nan

        # do error estimation as standard deviation of suitable realizations
        lambda_m_samples, flux_m_samples = x_values[samples], y_values[samples]
        lambda_m_err, flux_m_err = lambda_m_samples.std(), flux_m_samples.std()

        if (self.lambda_m_err != 'measure') and ((type(self.lambda_m_err) == type(1)) or (type(self.lambda_m_err) == type(1.1))):
            lambda_m_err = self.lambda_m_err

        return lambda_m, lambda_m_err, flux_m, flux_m_err

    def _measure_feature(self, feature):
        '''measure feature'''

        # run optimization if it has not already been done, and check if successful
        if np.isnan(self.continuum.loc[feature, 'wav1']):
            if not self._get_continuum(feature):
                return pd.Series([np.nan] * 6, index = ['pEW', 'e_pEW', 'vel', 'e_vel', 'abs', 'e_abs'])

        # get feature minimum
        selection = (self.wave > self.continuum.loc[feature, 'wav1']) & (self.wave < self.continuum.loc[feature, 'wav2'])
        lambda_m, lambda_m_err, flux_m, flux_m_err = self._get_feature_min(self.lines.loc[feature, 'rest_wavelength'],
                                                                           self.x[selection, 0],
                                                                           self.mod_mean[selection, 0], feature)

        self.continuum.loc[feature, ['wava', 'fluxa']] = lambda_m, flux_m

        # compute and store velocity
        velocity, velocity_err = get_speed(lambda_m, lambda_m_err, self.lines.loc[feature, 'rest_wavelength'])

        # if velocity is not detected, don't do pEW
        if np.isnan(velocity):
           return pd.Series([np.nan] * 6, index = ['pEW', 'e_pEW', 'vel', 'e_vel', 'abs', 'e_abs'])

        # compute pEWs
        if self.pEW_measure_from == 'model':
            pew_results, pew_err_results = pEW(self.x[:,0], self.mod_mean[:,0], self.continuum.loc[feature, 'cont'],
                                               np.array([self.continuum.loc[feature, ['wav1', 'wav2']],
                                                         self.continuum.loc[feature, ['flux1', 'flux2']]]),
                                               err_method = self.pEW_err_method, model = self.model,
                                               flux_err = self.flux_err)
        else:
            pew_results, pew_err_results = pEW(self.wave, self.flux, self.continuum.loc[feature, 'cont'],
                                               np.array([self.continuum.loc[feature, ['wav1', 'wav2']],
                                                         self.continuum.loc[feature, ['flux1', 'flux2']]]),
                                               err_method = self.pEW_err_method, model = self.model,
                                               flux_err = self.flux_err)

        # compute absorption depth
        a, a_err = absorption_depth(lambda_m, flux_m, flux_m_err, self.continuum.loc[feature, 'cont'])

        return pd.Series([pew_results, pew_err_results, velocity, velocity_err, a, a_err],
                         index = ['pEW', 'e_pEW', 'vel', 'e_vel', 'abs', 'e_abs'])

    def process_spectrum(self):
        '''do full processing of spectrum by measuring each feature'''

        self.results = self.lines.apply(lambda feature: self._measure_feature(feature.name), axis = 1, result_type = 'expand')

    def plot(self, initial_spec = True, model = True, continuum = True, lines = True, show_line_labels = True,
             save = False, display = True, **kwargs):
        '''make plot'''

        # check if plotting can be done
        if not hasattr(self, 'mod_mean'):
            warnings.warn('Cannot plot until spectrum has been processed!')
            return

        self.plotter = utils.setup_plot(**kwargs)
        if initial_spec:
            utils.plot_spec(self.plotter[1], self.wave, self.flux, spec_color = 'black', spec_alpha = 0.4)
        if model:
            utils.plot_filled_spec(self.plotter[1], self.x[:, 0], self.mod_mean[:, 0],
                                   self.conf[:, 0], fill_color = 'red', fill_alpha = 0.2)
        if continuum:
            utils.plot_continuum(self.plotter[1], self.continuum.loc[:, ['wav1', 'wav2', 'flux1', 'flux2']],
                                 cp_color = 'black', cl_color = 'blue', cl_alpha = 0.5)
        if model:
            utils.plot_spec(self.plotter[1], self.x, self.mod_mean, spec_color = 'red')
        if lines:
            utils.plot_lines(self.plotter[1], self.continuum.loc[:, ['wava', 'fluxa', 'cont']],
                             show_line_labels = show_line_labels)
        plt.tight_layout()
        if save is not False:
            self.plotter[0].savefig(save)
        elif display:
            self.plotter[0].show()

    def report(self):
        '''print report'''

        print(self.results.round(2).to_string())
