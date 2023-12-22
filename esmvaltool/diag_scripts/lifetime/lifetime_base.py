"""Base class for lifetime diagnostics."""

import logging
import os
import re

from pprint import pprint

import sys
import cartopy
import iris
import numpy as np
import matplotlib.pyplot as plt
import yaml
from copy import deepcopy
from iris.analysis import MEAN
from iris.util import broadcast_to_shape
from mapgenerator.plotting.timeseries import PlotSeries

from esmvaltool.diag_scripts.shared import ProvenanceLogger, names

logger = logging.getLogger(__name__)

def calculate_lifetime(dataset, plot_type, region):
    """Calculate the lifetime for the given plot_type and region.
    """
    # extract region from weights and reaction
    reaction = extract_region(dataset, region, case='reaction')
    weight = extract_region(dataset, region, case='weight')

    # calculate nominator and denominator
    # and sum of nominator and denominator via plot_type dimensions
    nominator = sum_up_to_plot_dimensions(weight, plot_type)
    denominator = sum_up_to_plot_dimensions(weight * reaction, plot_type)

    # division
    division = nominator / denominator

    return division

def extract_region(dataset, region, case='reaction'):
    """Return cube with everything outside region set to zero.

    If z_coord is defined as:
    - air_pressure, use:
                      - ptp and air_pressure
                      - tp_clim and air_pressure
    - atmosphere_hybrid_sigma_pressure_coordinate, use:
                      - tp_i and model_level_number
                      - ptp and (derived) air_pressure
                      - tp_clim and (derived) air_pressure

    Current aware regions:
    - TROP: troposphere (excl. tropopause), requires tropopause pressure
    - STRA: stratosphere (incl. tropopause), requires tropopause pressure
    """
    var = dataset[case]
    z_coord = dataset['z_coord']

    print(var.coords('air_pressure'))

    if not 'ptp' in dataset['variables'] and not 'tp_i' in dataset['variables']:
        tp_clim = climatological_tropopause(var[:,0,:,:])

    if region in ['TROP', 'STRA']:
    ## - can I choose the troposphere by the preprocessor?

        use_z_coord = 'air_pressure'
        if z_coord.name() == 'air_pressure':
            if 'ptp' in dataset['variables']:
                tp = dataset['variables']['ptp']
            else:
                tp = tp_clim
        elif z_coord.name() == 'atmosphere_hybrid_sigma_pressure_coordinate':
            if 'tp_i' in dataset['variables']:
                tp = dataset['variables']['tp_i']
                use_z_coord = 'model_level_number'
            elif 'ptp' in dataset['variables']:
                tp = dataset['variables']['ptp']
            else:
                tp = tp_clim

        z_4d = broadcast_to_shape(
            var.coord(use_z_coord).points,
            var.shape,
            var.coord_dims(use_z_coord)
        )

        tp_4d = broadcast_to_shape(
            tp.data,
            var.shape,
            var.coord_dims('time') + var.coord_dims('latitude') + var.coord_dims('longitude'),
        )

        if region == 'TROP':
            var.data = np.ma.array(
                var.data,
                mask=(z_4d <= tp_4d),
            )
        elif region == 'STRA':
            var.data = np.ma.array(
                var.data,
                mask=(z_4d > tp_4d),
            )
    else:
        raise NotImplementedError(f"region '{region}' is not supported")

    print(var.coords('air_pressure'))
    return var

def climatological_tropopause(cube):
    """Return cube with climatological tropopause pressure.

    """
    if not cube.coords('latitude', dim_coords=True):
        raise NotImplementedError("The provided cube must"
                                  " have a latitude cooridnate")

    latitude = broadcast_to_shape(
        cube.coord('latitude').points,
        cube.shape,
        cube.coord_dims('latitude')
    )

    tp_clim = cube.copy()
    tp_clim.data = (300. - 215. * (np.cos(np.deg2rad(latitude)) ** 2)) * 100.
    tp_clim.var_name = 'tp_clim'
    tp_clim.long_name = 'climatological tropopause pressure'
    tp_clim.units = 'Pa'

    return tp_clim

def sum_up_to_plot_dimensions(var, plot_type):
    """
    Returns the cube summed over the appropriate dimensions
    """
    if plot_type in ['timeseries', 'annual_cycle']:
        if var.coords('air_pressure', dim_coords=True):
            z_coord = var.coords('air_pressure', dim_coords=True)[0]
        elif var.coords('lev', dim_coords=True):
            z_coord = var.coords('lev', dim_coords=True)[0]
        elif var.coords('atmosphere_hybrid_sigma_pressure_coordinate', dim_coords=True):
            z_coord = var.coords('atmosphere_hybrid_sigma_pressure_coordinate', dim_coords=True)[0]

    if plot_type == 'timeseries':
        cube = var.collapsed(['longitude', 'latitude', z_coord], iris.analysis.SUM)
    elif plot_type == 'zonal_mean_profile':
        cube = var.collapsed(['longitude'], iris.analysis.SUM)
    elif plot_type == '1d_profile':
        cube = var.collapsed(['longitude', 'latitude'], iris.analysis.SUM)
    elif plot_type == 'annual_cycle':
        # TODO!
        # not iris.analysis.SUM but some kind of mean
        #cube = var.collapsed(['longitude', 'latitude', z_coord], iris.analysis.SUM)
        raise NotImplementedError(f"The sum to plot dimensions for plot_type {plot_type}"
                                  " is currently not implemented")


    return cube

def _replace_tags(paths, variable):
    """Replace tags in the config-developer's file with actual values."""
    if isinstance(paths, str):
        paths = set((paths.strip('/'), ))
    else:
        paths = set(path.strip('/') for path in paths)
    tlist = set()
    for path in paths:
        tlist = tlist.union(re.findall(r'{([^}]*)}', path))
    if 'sub_experiment' in variable:
        new_paths = []
        for path in paths:
            new_paths.extend(
                (re.sub(r'(\b{ensemble}\b)', r'{sub_experiment}-\1', path),
                 re.sub(r'({ensemble})', r'{sub_experiment}-\1', path)))
            tlist.add('sub_experiment')
        paths = new_paths

    for tag in tlist:
        original_tag = tag
        tag, _, _ = _get_caps_options(tag)

        if tag == 'latestversion':  # handled separately later
            continue
        if tag in variable:
            replacewith = variable[tag]
        else:
            raise ValueError(f"Dataset key '{tag}' must be specified for "
                             f"{variable}, check your recipe entry")
        paths = _replace_tag(paths, original_tag, replacewith)
    return paths


def _replace_tag(paths, tag, replacewith):
    """Replace tag by replacewith in paths."""
    _, lower, upper = _get_caps_options(tag)
    result = []
    if isinstance(replacewith, (list, tuple)):
        for item in replacewith:
            result.extend(_replace_tag(paths, tag, item))
    else:
        text = _apply_caps(str(replacewith), lower, upper)
        result.extend(p.replace('{' + tag + '}', text) for p in paths)
    return list(set(result))


def _get_caps_options(tag):
    lower = False
    upper = False
    if tag.endswith('.lower'):
        lower = True
        tag = tag[0:-6]
    elif tag.endswith('.upper'):
        upper = True
        tag = tag[0:-6]
    return tag, lower, upper


def _apply_caps(original, lower, upper):
    if lower:
        return original.lower()
    if upper:
        return original.upper()
    return original


class LifetimeBase():
    """Base class for lifetime diagnostic.

    It contains the common methods for path creation, provenance
    recording, option parsing and to create some common plots.

    """

    def __init__(self, config):
        self.cfg = config
        plot_folder = config.get(
            'plot_folder',
            '{plot_dir}/../../{dataset}/{exp}/{modeling_realm}/{real_name}',
        )
        plot_folder = plot_folder.replace('{plot_dir}',
                                          self.cfg[names.PLOT_DIR])
        self.plot_folder = os.path.abspath(plot_folder)
        self.plot_filename = config.get(
            'plot_filename',
            '{plot_type}_{real_name}_{dataset}_{mip}_{exp}_{ensemble}')
        self.plots = config.get('plots', {})
        default_config = os.path.join(os.path.dirname(__file__),
                                      "lifetime_config.yml")
        cartopy_data_dir = config.get('cartopy_data_dir', None)
        # if cartopy_data_dir:
        #     cartopy.config['data_dir'] = cartopy_data_dir
        with open(config.get('config_file', default_config)) as config_file:
            self.config = yaml.safe_load(config_file)

    def _add_file_extension(self, filename):
        """Add extension to plot filename."""
        return f"{filename}.{self.cfg['output_file_type']}"

    # def _get_proj_options(self, map_name):
    #     return self.config['maps'][map_name]

    def _get_variable_options(self, variable_group, map_name):
        options = self.config['variables'].get(
            variable_group, self.config['variables']['default'])
        if 'default' not in options:
            variable_options = options
        else:
            variable_options = options['default']
            if map_name in options:
                variable_options = {**variable_options, **options[map_name]}

        if 'bounds' in variable_options:
            if not isinstance(variable_options['bounds'], str):
                variable_options['bounds'] = [
                    float(n) for n in variable_options['bounds']
                ]
        logger.debug(variable_options)
        return variable_options

    def plot_timeseries(self, cube, var_info, period='', **kwargs):
        """Plot timeseries from a cube.

        It also automatically smoothes it for long timeseries of monthly data:
            - Between 10 and 70 years long, it also plots the 12-month rolling
              average along the raw series
            - For more than ten years, it plots the 12-month and 10-years
              rolling averages and not the raw series

        """
        if 'xlimits' not in kwargs:
            kwargs['xlimits'] = 'auto'
        length = cube.coord("year").points.max() - cube.coord(
            "year").points.min()
        filename = self.get_plot_path(f'timeseries{period}', var_info,
                                      add_ext=False)
        caption = ("{} of "
                   f"{var_info[names.LONG_NAME]} of dataset "
                   f"{var_info[names.DATASET]} (project "
                   f"{var_info[names.PROJECT]}) from "
                   f"{var_info[names.START_YEAR]} to "
                   f"{var_info[names.END_YEAR]}.")
        if length < 10 or length * 11 > cube.coord("year").shape[0]:
            self.plot_cube(cube, filename, **kwargs)
            self.record_plot_provenance(
                self._add_file_extension(filename),
                var_info,
                'timeseries',
                period=period,
                caption=caption.format("Time series"),
            )
        elif length < 70:
            self.plot_cube(cube, filename, **kwargs)
            self.record_plot_provenance(
                self._add_file_extension(filename),
                var_info,
                'timeseries',
                period=period,
                caption=caption.format("Time series"),
            )

            # Smoothed time series (12-month running mean)
            plt.gca().set_prop_cycle(None)
            self.plot_cube(cube.rolling_window('time', MEAN, 12),
                           f"{filename}_smoothed_12_months",
                           **kwargs)
            self.record_plot_provenance(
                self._add_file_extension(f"{filename}_smoothed_12_months"),
                var_info,
                'timeseries',
                period=period,
                caption=caption.format(
                    "Smoothed (12-months running mean) time series"),
            )
        else:
            # Smoothed time series (12-month running mean)
            self.plot_cube(cube.rolling_window('time', MEAN, 12),
                           f"{filename}_smoothed_12_months",
                           **kwargs)
            self.record_plot_provenance(
                self._add_file_extension(f"{filename}_smoothed_12_months"),
                var_info,
                'timeseries',
                period=period,
                caption=caption.format(
                    "Smoothed (12-months running mean) time series"),
            )

            # Smoothed time series (10-year running mean)
            self.plot_cube(cube.rolling_window('time', MEAN, 120),
                           f"{filename}_smoothed_10_years",
                           **kwargs)
            self.record_plot_provenance(
                self._add_file_extension(f"{filename}_smoothed_10_years"),
                var_info,
                'timeseries',
                period=period,
                caption=caption.format(
                    "Smoothed (10-years running mean) time series"),
            )

    def record_plot_provenance(self, filename, var_info, plot_type, **kwargs):
        """Write provenance info for a given file."""
        with ProvenanceLogger(self.cfg) as provenance_logger:
            prov = self.get_provenance_record(
                ancestor_files=[var_info['filename']],
                plot_type=plot_type,
                long_names=[var_info[names.LONG_NAME]],
                **kwargs,
            )
            provenance_logger.log(filename, prov)

    def plot_cube(self, cube, filename, linestyle='-', **kwargs):
        """Plot a timeseries from a cube.

        Supports multiplot layouts for cubes with extra dimensions
        `shape_id` or `region`.

        """
        plotter = PlotSeries()
        plotter.filefmt = self.cfg['output_file_type']
        plotter.img_template = filename
        region_coords = ('shape_id', 'region')

        for region_coord in region_coords:
            if cube.coords(region_coord):
                if cube.coord(region_coord).shape[0] > 1:
                    plotter.multiplot_cube(cube, 'time', region_coord,
                                           **kwargs)
                    return
        plotter.plot_cube(cube, 'time', linestyle=linestyle, **kwargs)

    @staticmethod
    def get_provenance_record(ancestor_files, **kwargs):
        """Create provenance record for the diagnostic data and plots."""
        record = {
            'authors': [
                'vegas-regidor_javier',
            ],
            'references': [
                'acknow_project',
            ],
            'ancestors': ancestor_files,
            **kwargs
        }
        return record

    def get_plot_path(self, plot_type, var_info, add_ext=True):
        """Get plot full path from variable info.

        Parameters
        ----------
        plot_type: str
            Name of the plot
        var_info: dict
            Variable information from ESMValTool
        add_ext: bool, optional (default: True)
            Add filename extension from configuration file.

        """
        return os.path.join(
            self.get_plot_folder(var_info),
            self.get_plot_name(plot_type, var_info, add_ext=add_ext),
        )

    def get_plot_folder(self, var_info):
        """Get plot storage folder from variable info.

        Parameters
        ----------
        var_info: dict
            Variable information from ESMValTool

        """
        info = {
            'real_name': self._real_name(var_info['variable_group']),
            **var_info
        }
        folder = os.path.expandvars(
            os.path.expanduser(
                list(_replace_tags(self.plot_folder, info))[0]
            )
        )
        if self.plot_folder.startswith('/'):
            folder = '/' + folder
        if not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        return folder

    def get_plot_name(self, plot_type, var_info, add_ext=True):
        """Get plot filename from variable info.

        Parameters
        ----------
        plot_type: str
            Name of the plot
        var_info: dict
            Variable information from ESMValTool
        add_ext: bool, optional (default: True)
            Add filename extension from configuration file.

        """
        info = {
            "plot_type": plot_type,
            'real_name': self._real_name(var_info['variable_group']),
            **var_info
        }
        file_name = list(_replace_tags(self.plot_filename, info))[0]
        if add_ext:
            file_name = self._add_file_extension(file_name)
        return file_name

    @staticmethod
    def _set_rasterized(axes=None):
        """Rasterize all artists and collection of axes if desired."""
        if axes is None:
            axes = plt.gca()
        if not isinstance(axes, list):
            axes = [axes]
        for single_axes in axes:
            for artist in single_axes.artists:
                artist.set_rasterized(True)
            for collection in single_axes.collections:
                collection.set_rasterized(True)

    @staticmethod
    def _real_name(variable_group):
        for subfix in ('Ymean', 'Ysum', 'mean', 'sum'):
            if variable_group.endswith(subfix):
                variable_group = variable_group.replace(subfix, '')
        return variable_group
