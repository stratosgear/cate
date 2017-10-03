# The MIT License (MIT)
# Copyright (c) 2016, 2017 by the ESA CCI Toolbox development team and contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Description
===========

This plugin module adds the local data source to the data store registry.

Verification
============

The module's unit-tests are located in
`test/ds/test_esa_cci_ftp.py <https://github.com/CCI-Tools/cate/blob/master/test/ds/test_esa_cci_ftp.py>`_
and may be executed using ``$ py.test test/ds/test_esa_cci_ftp.py --cov=cate/ds/esa_cci_ftp.py``
for extra code coverage information.

Components
==========
"""

import json
import os
import psutil
import shutil
import uuid
import warnings
import xarray as xr
from collections import OrderedDict
from datetime import datetime
from dateutil import parser
from glob import glob
from math import ceil, floor, isnan
from typing import Optional, Sequence, Union, Any, Tuple
from xarray.backends import NetCDF4DataStore

from cate.conf import get_config_value, get_data_stores_path
from cate.conf.defaults import NETCDF_COMPRESSION_LEVEL
from cate.core.ds import DATA_STORE_REGISTRY, DataAccessError, DataAccessWarning, DataStore, DataSource, \
    open_xarray_dataset
from cate.core.types import Polygon, PolygonLike, TimeRange, TimeRangeLike, VarNames, VarNamesLike
from cate.util.monitor import Monitor

__author__ = "Norman Fomferra (Brockmann Consult GmbH), " \
             "Marco Zühlke (Brockmann Consult GmbH), " \
             "Chris Bernat (Telespazio VEGA UK Ltd)"

_REFERENCE_DATA_SOURCE_TYPE = "FILE_PATTERN"

_NAMESPACE = uuid.UUID(bytes=b"1234567890123456", version=3)


def get_data_store_path():
    return os.environ.get('CATE_LOCAL_DATA_STORE_PATH',
                          os.path.join(get_data_stores_path(), 'local'))


def add_to_data_store_registry():
    data_store = LocalDataStore('local', get_data_store_path())
    DATA_STORE_REGISTRY.add_data_store(data_store)


# TODO (kbernat): document this class
class LocalDataSource(DataSource):
    """

    :param ds_id:
    :param files:
    :param data_store:
    :param temporal_coverage:
    :param spatial_coverage:
    :param variables:
    :param meta_info:
    """

    def __init__(self,
                 ds_id: str,
                 files: Union[Sequence[str], OrderedDict],
                 data_store: 'LocalDataStore',
                 temporal_coverage: TimeRangeLike.TYPE = None,
                 spatial_coverage: PolygonLike.TYPE = None,
                 variables: VarNamesLike.TYPE = None,
                 meta_info: dict = None):
        self._id = ds_id
        if isinstance(files, Sequence):
            self._files = OrderedDict.fromkeys(files)
        else:
            self._files = files
        self._data_store = data_store

        initial_temporal_coverage = TimeRangeLike.convert(temporal_coverage) if temporal_coverage else None
        if not initial_temporal_coverage:
            files_number = len(self._files.items())
            if files_number > 0:
                files_range = list(self._files.values())
                if files_range:
                    if isinstance(files_range[0], Tuple):
                        initial_temporal_coverage = TimeRangeLike.convert(tuple([files_range[0][0],
                                                                                 files_range[files_number - 1][1]]))
                    elif isinstance(files_range[0], datetime):
                        initial_temporal_coverage = TimeRangeLike.convert((files_range[0],
                                                                           files_range[files_number - 1]))

        self._temporal_coverage = initial_temporal_coverage
        self._spatial_coverage = PolygonLike.convert(spatial_coverage) if spatial_coverage else None
        self._variables = VarNamesLike.convert(variables) if variables else []

        self._meta_info = meta_info if meta_info else OrderedDict()

        if self._variables and not self._meta_info.get('variables', None):
            self._meta_info['variables'] = [
                {'name': var_name,
                 'units': '',
                 'long_name': '',
                 'standard_name': ''
                 } for var_name in self._variables]

        self._is_complete = True

    def _resolve_file_path(self, path) -> Sequence:
        return glob(os.path.join(self._data_store.data_store_path, path))

    def open_dataset(self,
                     time_range: TimeRangeLike.TYPE = None,
                     region: PolygonLike.TYPE = None,
                     var_names: VarNamesLike.TYPE = None,
                     protocol: str = None) -> Any:
        time_range = TimeRangeLike.convert(time_range) if time_range else None
        if region:
            region = PolygonLike.convert(region)
        if var_names:
            var_names = VarNamesLike.convert(var_names)
        paths = []
        if time_range:
            time_series = list(self._files.values())
            file_paths = list(self._files.keys())
            for i in range(len(time_series)):
                if time_series[i]:
                    if isinstance(time_series[i], Tuple) and \
                                    time_series[i][0] >= time_range[0] and \
                                    time_series[i][1] <= time_range[1]:
                        paths.extend(self._resolve_file_path(file_paths[i]))
                    elif isinstance(time_series[i], datetime) and time_range[0] <= time_series[i] < time_range[1]:
                        paths.extend(self._resolve_file_path(file_paths[i]))
        else:
            for file in self._files.items():
                paths.extend(self._resolve_file_path(file[0]))
        if paths:
            paths = sorted(set(paths))
            try:
                ds = open_xarray_dataset(paths)
                if region:
                    [lon_min, lat_min, lon_max, lat_max] = region.bounds
                    ds = ds.sel(drop=False, lat=slice(lat_min, lat_max), lon=slice(lon_min, lon_max))
                if var_names:
                    ds = ds.drop([var_name for var_name in ds.variables.keys() if var_name not in var_names])
                return ds
            except OSError as e:
                if time_range:
                    raise DataAccessError(self, "Cannot open local dataset for time range: {}\n"
                                                "Error details: {}"
                                          .format(TimeRangeLike.format(time_range), e))
                else:
                    raise DataAccessError(self, "Cannot open local dataset\n"
                                                "Error details: {}"
                                          .format(TimeRangeLike.format(time_range), e))
        else:
            if time_range:
                raise DataAccessError(self, "No data sets available for specified time range {}".format(
                    TimeRangeLike.format(time_range)), paths)
            else:
                raise DataAccessError(self, "No data sets available")

    @staticmethod
    def _get_harmonized_coordinate_value(attrs: dict, attr_name: str):
        value = attrs.get(attr_name, 'nan')
        if isinstance(value, str):
            return float(value.rstrip('degrees').rstrip('f'))
        return value

    def _make_local(self,
                    local_ds: 'LocalDataSource',
                    time_range: TimeRangeLike.TYPE = None,
                    region: PolygonLike.TYPE = None,
                    var_names: VarNamesLike.TYPE = None,
                    monitor: Monitor = Monitor.NONE):

        local_id = local_ds.id

        time_range = TimeRangeLike.convert(time_range) if time_range else None
        region = PolygonLike.convert(region) if region else None
        var_names = VarNamesLike.convert(var_names) if var_names else None  # type: Sequence

        compression_level = get_config_value('NETCDF_COMPRESSION_LEVEL', NETCDF_COMPRESSION_LEVEL)
        compression_enabled = True if compression_level > 0 else False

        encoding_update = dict()
        if compression_enabled:
            encoding_update.update({'zlib': True, 'complevel': compression_level})

        local_path = os.path.join(local_ds.data_store.data_store_path, local_id)
        data_store_path = local_ds.data_store.data_store_path
        if not os.path.exists(local_path):
            os.makedirs(local_path)

        monitor.start("Sync " + self.id, total_work=len(self._files.items()))
        for remote_relative_filepath, coverage in self._files.items():
            child_monitor = monitor.child(work=1)

            file_name = os.path.basename(remote_relative_filepath)
            local_relative_filepath = os.path.join(local_id, file_name)
            local_absolute_filepath = os.path.join(data_store_path, local_relative_filepath)

            remote_absolute_filepath = os.path.join(self._data_store.data_store_path, remote_relative_filepath)

            if isinstance(coverage, Tuple):

                time_coverage_start = coverage[0]
                time_coverage_end = coverage[1]

                remote_netcdf = None
                local_netcdf = None
                if not time_range or time_coverage_start >= time_range[0] and time_coverage_end <= time_range[1]:
                    if region or var_names:
                        try:
                            remote_netcdf = NetCDF4DataStore(remote_absolute_filepath)

                            local_netcdf = NetCDF4DataStore(local_absolute_filepath, mode='w', persist=True)
                            local_netcdf.set_attributes(remote_netcdf.get_attrs())

                            remote_dataset = xr.Dataset.load_store(remote_netcdf)

                            geo_lat_min = None
                            geo_lat_max = None
                            geo_lon_min = None
                            geo_lon_max = None

                            process_region = False
                            if region:
                                geo_lat_min = self._get_harmonized_coordinate_value(remote_dataset.attrs,
                                                                                    'geospatial_lat_min')
                                geo_lat_max = self._get_harmonized_coordinate_value(remote_dataset.attrs,
                                                                                    'geospatial_lat_max')
                                geo_lon_min = self._get_harmonized_coordinate_value(remote_dataset.attrs,
                                                                                    'geospatial_lon_min')
                                geo_lon_max = self._get_harmonized_coordinate_value(remote_dataset.attrs,
                                                                                    'geospatial_lon_max')

                                geo_lat_res = self._get_harmonized_coordinate_value(remote_dataset.attrs,
                                                                                    'geospatial_lon_resolution')
                                geo_lon_res = self._get_harmonized_coordinate_value(remote_dataset.attrs,
                                                                                    'geospatial_lat_resolution')
                                if not (isnan(geo_lat_min) or isnan(geo_lat_max) or isnan(geo_lon_min) or
                                        isnan(geo_lon_max) or isnan(geo_lat_res) or isnan(geo_lon_res)):
                                    process_region = True

                                    [lon_min, lat_min, lon_max, lat_max] = region.bounds

                                    descending_data_order = set()
                                    for var in remote_dataset.coords.keys():
                                        if remote_dataset.coords[var][0] > remote_dataset.coords[var][-1]:
                                            descending_data_order.add(var)

                                    if 'lat' not in descending_data_order:
                                        lat_min = lat_min - geo_lat_min
                                        lat_max = lat_max - geo_lat_min
                                    else:
                                        lat_min_copy = lat_min
                                        lat_min = geo_lat_max - lat_max
                                        lat_max = geo_lat_max - lat_min_copy

                                    if 'lon' not in descending_data_order:
                                        lon_min = lon_min - geo_lon_min
                                        lon_max = lon_max - geo_lon_min
                                    else:
                                        lon_min_copy = lon_min
                                        lon_min = geo_lon_max - lon_max
                                        lon_max = geo_lon_max - lon_min_copy

                                    lat_min = int(floor(lat_min / geo_lat_res))
                                    lat_max = int(ceil(lat_max / geo_lat_res))
                                    lon_min = int(floor(lon_min / geo_lon_res))
                                    lon_max = int(ceil(lon_max / geo_lon_res))

                                    remote_dataset = remote_dataset.isel(drop=False,
                                                                         lat=slice(lat_min, lat_max),
                                                                         lon=slice(lon_min, lon_max))
                                    if 'lat' not in descending_data_order:
                                        geo_lat_min_copy = geo_lat_min
                                        geo_lat_min = lat_min * geo_lat_res + geo_lat_min_copy
                                        geo_lat_max = lat_max * geo_lat_res + geo_lat_min_copy
                                    else:
                                        geo_lat_max_copy = geo_lat_max
                                        geo_lat_min = geo_lat_max_copy - lat_max * geo_lat_res
                                        geo_lat_max = geo_lat_max_copy - lat_min * geo_lat_res

                                    if 'lon' not in descending_data_order:
                                        geo_lon_min_copy = geo_lon_min
                                        geo_lon_min = lon_min * geo_lon_res + geo_lon_min_copy
                                        geo_lon_max = lon_max * geo_lon_res + geo_lon_min_copy
                                    else:
                                        geo_lon_max_copy = geo_lon_max
                                        geo_lon_min = geo_lon_max_copy - lon_max * geo_lon_res
                                        geo_lon_max = geo_lon_max_copy - lon_min * geo_lon_res

                            if not var_names:
                                var_names = [var_name for var_name in remote_netcdf.variables.keys()]
                            var_names.extend([coord_name for coord_name in remote_dataset.coords.keys()
                                              if coord_name not in var_names])
                            child_monitor.start(label=file_name, total_work=len(var_names))
                            for sel_var_name in var_names:
                                var_dataset = remote_dataset.drop(
                                    [var_name for var_name in remote_dataset.variables.keys() if
                                     var_name != sel_var_name])
                                if compression_enabled:
                                    var_dataset.variables.get(sel_var_name).encoding.update(encoding_update)
                                local_netcdf.store_dataset(var_dataset)
                                child_monitor.progress(work=1, msg=sel_var_name)
                            if process_region:
                                local_netcdf.set_attribute('geospatial_lat_min', geo_lat_min)
                                local_netcdf.set_attribute('geospatial_lat_max', geo_lat_max)
                                local_netcdf.set_attribute('geospatial_lon_min', geo_lon_min)
                                local_netcdf.set_attribute('geospatial_lon_max', geo_lon_max)
                        finally:
                            if remote_netcdf:
                                remote_netcdf.close()
                            if local_netcdf:
                                local_netcdf.close()
                                local_ds.add_dataset(local_relative_filepath, (time_coverage_start, time_coverage_end))
                        child_monitor.done()
                    else:
                        shutil.copy(remote_absolute_filepath, local_absolute_filepath)
                        local_ds.add_dataset(local_relative_filepath, (time_coverage_start, time_coverage_end))
                        child_monitor.done()
        monitor.done()
        return local_id

    def make_local(self,
                   local_name: str,
                   local_id: str = None,
                   time_range: TimeRangeLike.TYPE = None,
                   region: PolygonLike.TYPE = None,
                   var_names: VarNamesLike.TYPE = None,
                   monitor: Monitor = Monitor.NONE) -> Optional[DataSource]:

        time_range = TimeRangeLike.convert(time_range) if time_range else None
        region = PolygonLike.convert(region) if region else None
        var_names = VarNamesLike.convert(var_names) if var_names else None

        local_store = DATA_STORE_REGISTRY.get_data_store('local')
        if not local_store:
            add_to_data_store_registry()
            local_store = DATA_STORE_REGISTRY.get_data_store('local')
        if not local_store:
            raise ValueError('Cannot initialize `local` DataStore')

        _uuid = LocalDataStore.generate_uuid(ref_id=self.id, time_range=time_range, region=region, var_names=var_names)

        if not local_name or len(local_name) == 0:
            local_name = "local.{}.{}".format(self.id, _uuid)
            existing_ds_list = local_store.query(ds_id=local_name)
            if len(existing_ds_list) == 1:
                return existing_ds_list[0]
        else:
            existing_ds_list = local_store.query(ds_id='local.%s' % local_name)
            if len(existing_ds_list) == 1:
                if existing_ds_list[0].meta_info.get('uuid', None) == _uuid:
                    return existing_ds_list[0]
                else:
                    raise ValueError('Datastore {} already contains dataset {}'.format(local_store.id, local_name))

        local_meta_info = self.meta_info.copy()
        local_meta_info['ref_uuid'] = local_meta_info.get('uuid', None)
        local_meta_info['uuid'] = _uuid

        local_ds = local_store.create_data_source(local_name, region, local_name,
                                                  time_range=time_range, var_names=var_names,
                                                  meta_info=self.meta_info.copy())
        if local_ds:
            if not local_ds.is_complete:
                self._make_local(local_ds, time_range, region, var_names, monitor=monitor)

            if local_ds.is_empty:
                local_store.remove_data_source(local_ds)
                return None

            local_store.register_ds(local_ds)
            return local_ds
        return None

    def add_dataset(self, file, time_coverage: TimeRangeLike.TYPE = None, update: bool = False,
                    extract_meta_info: bool = False):
        if update or self._files.keys().isdisjoint([file]):
            self._files[file] = time_coverage
            if time_coverage:
                self._extend_temporal_coverage(time_coverage)
        self._files = OrderedDict(sorted(self._files.items(),
                                         key=lambda f: f[1] if isinstance(f, Tuple) and f[1] else datetime.max))
        if extract_meta_info:
            try:
                ds = xr.open_dataset(file)
                self._meta_info.update(ds.attrs)
            except OSError:
                pass
        self.save()

    def _extend_temporal_coverage(self, time_range: TimeRangeLike.TYPE):
        """

        :param time_range: Time range to be added to data source temporal coverage
        :return:
        """
        if not time_range:
            return
        if self._temporal_coverage:
            if time_range[0] >= self._temporal_coverage[1]:
                self._temporal_coverage = tuple([self._temporal_coverage[0], time_range[1]])
            elif time_range[1] <= self._temporal_coverage[0]:
                self._temporal_coverage = tuple([time_range[0], self._temporal_coverage[1]])
        else:
            self._temporal_coverage = time_range
        self.save()

    def update_temporal_coverage(self, time_range: TimeRangeLike.TYPE):
        """

        :param time_range: Time range to be added to data source temporal coverage
        :return:
        """
        self._extend_temporal_coverage(time_range)

    def _reduce_temporal_coverage(self, time_range: TimeRangeLike.TYPE):
        """

        :param time_range:Time range to be removed from data source temporal coverage
        :return:
        """
        if not time_range or not self._temporal_coverage:
            return
        if time_range[0] > self._temporal_coverage[0] and time_range[1] == self._temporal_coverage[1]:
            self._temporal_coverage = (self._temporal_coverage[0], time_range[0])
        if time_range[1] < self._temporal_coverage[1] and time_range[0] == self._temporal_coverage[0]:
            self._temporal_coverage = (time_range[1], self._temporal_coverage[1])

    def reduce_temporal_coverage(self, time_coverage: TimeRangeLike.TYPE):
        files_to_remove = []
        time_range_to_be_removed = None
        for file, time_range in self._files.items():
            if time_coverage[0] <= time_range[0] <= time_coverage[1] \
                    and time_coverage[0] <= time_range[1] <= time_coverage[1]:
                files_to_remove.append(file)
                if not time_range_to_be_removed and isinstance(time_range, Tuple):
                    time_range_to_be_removed = time_range
                else:
                    time_range_to_be_removed = (time_range_to_be_removed[0], time_range[1])
            elif time_coverage[0] <= time_range[0] <= time_coverage[1]:
                time_range_to_be_removed = (time_range_to_be_removed[0], time_range[0])
            elif time_coverage[0] <= time_range[1] <= time_coverage[1]:
                time_range_to_be_removed = time_range[1], time_coverage[1]
        for file in files_to_remove:
            os.remove(os.path.join(self._data_store.data_store_path, file))
            del self._files[file]
        if time_range_to_be_removed:
            self._reduce_temporal_coverage(time_range_to_be_removed)

    def save(self, unlock: bool = False):
        self._data_store.save_data_source(self, unlock)

    def temporal_coverage(self, monitor: Monitor = Monitor.NONE) -> Optional[TimeRange]:
        return self._temporal_coverage

    def spatial_coverage(self):
        if not self._spatial_coverage and \
                set(self._meta_info.keys()).issuperset({'bbox_minx', 'bbox_miny', 'bbox_maxx', 'bbox_maxy'}):
            self._spatial_coverage = PolygonLike.convert(",".join([
                self._meta_info.get('bbox_minx'),
                self._meta_info.get('bbox_miny'),
                self._meta_info.get('bbox_maxx'),
                self._meta_info.get('bbox_maxy')])
            )
        return self._spatial_coverage

    @property
    def data_store(self) -> 'LocalDataStore':
        return self._data_store

    @property
    def id(self) -> str:
        return self._id

    @property
    def meta_info(self) -> OrderedDict:
        return self._meta_info

    @property
    def variables_info(self):
        return self._meta_info.get('variables', [])

    @property
    def info_string(self):
        return 'Files: %s' % (' '.join(self._files))

    @property
    def is_complete(self) -> bool:
        """
        Return a DataSource creation state
        :return:
        """
        return self._is_complete

    @property
    def is_empty(self) -> bool:
        """
        Check if DataSource is empty

        """
        return not self._files or len(self._files) == 0

    def set_completed(self, state: bool):
        """
        Sets state of DataSource creation/completion
        :param state: Is DataSource completed
        :return:
        """
        self._is_complete = state

    def _repr_html_(self):
        import html
        return '<table style="border:0;">\n' \
               '<tr><td>Name</td><td><strong>%s</strong></td></tr>\n' \
               '<tr><td>Files</td><td><strong>%s</strong></td></tr>\n' \
               '</table>\n' % (html.escape(self._id), html.escape(' '.join(self._files)))

    def to_json_dict(self):
        """
        Return a JSON-serializable dictionary representation of this object.

        :return: A JSON-serializable dictionary
        """
        config = OrderedDict({
            'name': self._id,
            'meta_info': self._meta_info,
            'files': [[item[0], item[1][0], item[1][1]] if item[1] else [item[0]] for item in self._files.items()]
        })
        return config

    @classmethod
    def from_json_dict(cls, json_dict: dict, data_store: 'LocalDataStore') -> Optional['LocalDataSource']:

        name = json_dict.get('name')
        files = json_dict.get('files', None)

        variables = []
        temporal_coverage = None
        spatial_coverage = None

        meta_info = json_dict.get('meta_info', OrderedDict())

        meta_data = json_dict.get('meta_data', None)
        if meta_data:
            temporal_coverage = meta_data.get('temporal_coverage', meta_data.get('temporal_covrage', None))
            spatial_coverage = meta_data.get('spatial_coverage', None)
            variables = meta_data.get('variables', None)

        if meta_info:
            if not variables:
                variables = [v.get('name') for v in meta_info.get('variables', dict()) if not v.get('name', None)]
            if not temporal_coverage:
                temporal_coverage_start = meta_info.get('temporal_coverage_start', None)
                temporal_coverage_end = meta_info.get('temporal_coverage_end', None)
                if temporal_coverage_start and temporal_coverage_end:
                    temporal_coverage = temporal_coverage_start, temporal_coverage_end

        files_dict = OrderedDict()
        if name and isinstance(files, list):
            if len(files) > 0:
                if isinstance(files[0], list):
                    file_details_length = len(files[0])
                    if file_details_length > 2:
                        files_dict = OrderedDict((item[0], (parser.parse(item[1]).replace(microsecond=0),
                                                            parser.parse(item[2]).replace(microsecond=0))
                                                  if item[1] and item[2] else None) for item in files)
                    elif file_details_length > 0:
                        files_dict = OrderedDict((item[0], parser.parse(item[1]).replace(microsecond=0))
                                                 if len(item) > 1 else (item[0], None) for item in files)
                else:
                    files_dict = files
        return LocalDataSource(name, files_dict, data_store, temporal_coverage, spatial_coverage, variables,
                               meta_info=meta_info)


class LocalDataStore(DataStore):
    def __init__(self, ds_id: str, store_dir: str):
        super().__init__(ds_id, title='Local Data Sources', is_local=True)
        self._store_dir = store_dir
        self._data_sources = None

    def add_pattern(self, data_source_id: str, files: Union[str, Sequence[str]] = None) -> 'DataSource':
        data_source = self.create_data_source(data_source_id)
        if isinstance(files, str) and len(files) > 0:
            files = [files]
        is_first_file = True
        if not files:
            raise ValueError("files pattern cannot be empty")
        for file in files:
            if is_first_file:
                data_source.add_dataset(file, extract_meta_info=True)
                is_first_file = False
            else:
                data_source.add_dataset(file)

        self.register_ds(data_source)
        return data_source

    def remove_data_source(self, data_source: Union[str, DataSource], remove_files: bool = True):
        if isinstance(data_source, str):
            data_sources = self.query(ds_id=data_source)
            if not data_sources or len(data_sources) != 1:
                return
            data_source = data_sources[0]
        file_name = os.path.join(self._store_dir, data_source.id + '.json')
        if os.path.isfile(file_name):
            os.remove(file_name)
        lock_file = os.path.join(self._store_dir, data_source.id + '.lock')
        if os.path.isfile(lock_file):
            os.remove(lock_file)
        if remove_files:
            data_source_path = os.path.join(self._store_dir, data_source.id)
            if os.path.isdir(data_source_path):
                shutil.rmtree(os.path.join(self._store_dir, data_source.id), ignore_errors=True)
        if data_source in self._data_sources:
            self._data_sources.remove(data_source)

    def register_ds(self, data_source: LocalDataSource):
        data_source.set_completed(True)
        self._data_sources.append(data_source)

    @classmethod
    def generate_uuid(cls, ref_id: str,
                      time_range: Optional[TimeRange] = None,
                      region: Optional[Polygon] = None,
                      var_names: Optional[VarNames] = None) -> str:

        if time_range:
            ref_id += TimeRangeLike.format(time_range)
        if region:
            ref_id += PolygonLike.format(region)
        if var_names:
            ref_id += VarNamesLike.format(var_names)

        return str(uuid.uuid3(_NAMESPACE, ref_id))

    @classmethod
    def generate_title(cls, title: str,
                       time_range: Optional[TimeRange] = None,
                       region: Optional[Polygon] = None,
                       var_names: Optional[VarNames] = None) -> str:

        if time_range:
            title += " [TimeRange:{}]".format(TimeRangeLike.format(time_range))
        if region:
            title += " [Region:{}]".format(PolygonLike.format(region))
        if var_names:
            title += " [Variables:{}]".format(VarNamesLike.format(var_names))

        return title

    def create_data_source(self, data_source_id: str, region: PolygonLike.TYPE = None,
                           title: str = None,
                           time_range: TimeRangeLike.TYPE = None, var_names: VarNamesLike.TYPE = None,
                           meta_info: OrderedDict = None, lock_file: bool = False):
        self._init_data_sources()

        if title:
            if not meta_info:
                meta_info = OrderedDict()
            meta_info['title'] = title

        if not data_source_id.startswith('%s.' % self.id):
            data_source_id = '%s.%s' % (self.id, data_source_id)

        lock_filename = '{}.lock'.format(data_source_id)
        lock_filepath = os.path.join(self._store_dir, lock_filename)
        pid = os.getpid()
        create_time = int(psutil.Process(pid).create_time() * 1_000_000)

        data_source = None
        for ds in self._data_sources:
            if ds.id == data_source_id:
                if lock_file and os.path.isfile(lock_filepath):
                    with open(lock_filepath, 'r') as lock_file:
                        writer_pid = lock_file.readline()
                        if writer_pid:
                            writer_create_time = -1
                            writer_pid, writer_timestamp = [(int(val) for val in writer_pid.split(":"))
                                                            if ":" in writer_pid else writer_pid, writer_create_time]
                            if psutil.pid_exists(writer_pid) and writer_pid != pid:
                                if writer_timestamp > writer_create_time:
                                    writer_create_time = int(psutil.Process(writer_pid).create_time() * 1_000_000)
                                if writer_create_time == writer_timestamp:
                                    raise DataAccessError(self, "Data source '{}' is currently being created by other "
                                                                "process (pid:{})". format(ds.id, writer_pid))
                            # ds.temporal_coverage() == time_range and
                            if ds.spatial_coverage() == region \
                                    and ds.variables_info == var_names:
                                data_source = ds
                                data_source.set_completed(False)
                                break
                raise DataAccessError(self, "Data source '{}' already exists.". format(data_source_id))
        if not data_source:
            data_source = LocalDataSource(data_source_id, files=[], data_store=self, spatial_coverage=region,
                                          variables=var_names, temporal_coverage=time_range, meta_info=meta_info)
            data_source.set_completed(False)
            self._save_data_source(data_source)

        if lock_file:
            with open(lock_filepath, 'w') as lock_file:
                lock_file.write("{}:{}".format(pid, create_time))

        return data_source

    @property
    def data_store_path(self):
        """Path to directory that stores the local data source files."""
        return self._store_dir

    def query(self, ds_id: str = None, query_expr: str = None, monitor: Monitor = Monitor.NONE) \
            -> Sequence[LocalDataSource]:
        self._init_data_sources()
        if ds_id or query_expr:
            return [ds for ds in self._data_sources if ds.matches(ds_id=ds_id, query_expr=query_expr)]
        return self._data_sources

    def __repr__(self):
        return "LocalFilePatternDataStore(%s)" % repr(self.id)

    def _repr_html_(self):
        self._init_data_sources()
        rows = []
        row_count = 0
        for data_source in self._data_sources:
            row_count += 1
            # noinspection PyProtectedMember
            rows.append('<tr><td><strong>%s</strong></td><td>%s</td></tr>' % (row_count, data_source._repr_html_()))
        return '<p>Contents of LocalFilePatternDataStore "%s"</p><table>%s</table>' % (self.id, '\n'.join(rows))

    def _init_data_sources(self, skip_broken: bool=True):
        """

        :param skip_broken: In case of broken data sources skip loading and log warning instead of rising Error.
        :return:
        """
        if self._data_sources:
            return
        os.makedirs(self._store_dir, exist_ok=True)
        json_files = [f for f in os.listdir(self._store_dir)
                      if os.path.isfile(os.path.join(self._store_dir, f)) and f.endswith('.json')]
        unfinished_ds = [f for f in os.listdir(self._store_dir)
                         if os.path.isfile(os.path.join(self._store_dir, f)) and f.endswith('.lock')]
        json_files = [f for f in json_files if f.replace('.json', '.lock') not in unfinished_ds]
        self._data_sources = []
        for json_file in json_files:
            try:
                data_source = self._load_data_source(os.path.join(self._store_dir, json_file))
                if data_source:
                    self._data_sources.append(data_source)
            except DataAccessError as e:
                if skip_broken:
                    warnings.warn(e.cause, DataAccessWarning, stacklevel=0)
                else:
                    raise e

    def save_data_source(self, data_source, unlock: bool = False):
        self._save_data_source(data_source)
        if unlock:
            lock_file = os.path.join(self._store_dir, data_source.id + '.lock')
            if os.path.isfile(lock_file):
                os.remove(lock_file)

    def _save_data_source(self, data_source):
        json_dict = data_source.to_json_dict()
        dump_kwargs = dict(indent='  ', default=self._json_default_serializer)
        file_name = os.path.join(self._store_dir, data_source.id + '.json')
        try:
            with open(file_name, 'w') as fp:
                json.dump(json_dict, fp, **dump_kwargs)
        except EnvironmentError as e:
            raise DataAccessError(self, "Couldn't save Data Source config file {}\n{}".format(file_name, e.strerror))

    def _load_data_source(self, json_path):
        json_dict = self._load_json_file(json_path)
        if json_dict:
            return LocalDataSource.from_json_dict(json_dict, self)

    @staticmethod
    def _load_json_file(json_path: str):
        if os.path.isfile(json_path):
            try:
                with open(json_path) as fp:
                    return json.load(fp=fp) or {}
            except json.decoder.JSONDecodeError:
                raise DataAccessError(None, "Cannot load data source config, {}".format(json_path))
        else:
            raise DataAccessError(None, "Data source config does not exists, {}".format(json_path))

    @staticmethod
    def _json_default_serializer(obj):
        if isinstance(obj, datetime):
            return obj.replace(microsecond=0).isoformat()
        # if isinstance(obj, Polygon):
        #    return str(obj.bounds).replace(' ', '').replace('(', '\"').replace(')', '\"'))
        raise TypeError('Not sure how to serialize %s' % (obj,))
