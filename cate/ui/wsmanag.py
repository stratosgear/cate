# The MIT License (MIT)
# Copyright (c) 2016 by the Cate Development Team and contributors
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

import json
import os
import pprint
import shutil
import urllib.parse
import urllib.request
from abc import ABCMeta, abstractmethod
from collections import OrderedDict
from typing import List

from cate.core.monitor import Monitor
from cate.core.objectio import write_object
from cate.core.util import UNDEFINED
from cate.core.util import encode_url_path
from cate.core.workflow import Workflow

from .workspace import Workspace, WorkspaceError

WORKSPACE_DATA_DIR_NAME = '.cate-workspace'
WORKSPACE_WORKFLOW_FILE_NAME = 'workflow.json'

# TODO (forman, 20160908): implement file lock for opened workspaces (issue #26)
# TODO (forman, 20160928): must turn all WebAPI handler into asynchronous tasks (issue #51)


# {{cate-config}}
# allow two minutes timeout for any synchronous workspace I/O
WORKSPACE_TIMEOUT = 2 * 60.0

# {{cate-config}}
# allow one hour timeout for any synchronous resource processing
RESOURCE_TIMEOUT = 60 * 60.0

# {{cate-config}}
# allow one hour extra timeout for matplotlib to block the WebAPI service's main thread by showing a Qt window
PLOT_TIMEOUT = 60 * 60.0


class WorkspaceManager(metaclass=ABCMeta):
    """
    Abstract base class which represents the ``WorkspaceManager`` interface.
    """

    @abstractmethod
    def get_open_workspaces(self) -> List[Workspace]:
        pass

    @abstractmethod
    def get_workspace(self, base_dir: str, open: bool = False) -> Workspace:
        pass

    @abstractmethod
    def new_workspace(self, base_dir: str, save: bool = False, description: str = None) -> Workspace:
        pass

    @abstractmethod
    def open_workspace(self, base_dir: str) -> Workspace:
        pass

    @abstractmethod
    def close_workspace(self, base_dir: str, save: bool) -> None:
        pass

    @abstractmethod
    def close_all_workspaces(self, save: bool) -> None:
        pass

    @abstractmethod
    def save_workspace(self, base_dir: str) -> None:
        pass

    @abstractmethod
    def save_all_workspaces(self) -> None:
        pass

    @abstractmethod
    def delete_workspace(self, base_dir: str) -> None:
        pass

    @abstractmethod
    def clean_workspace(self, base_dir: str) -> None:
        pass

    @abstractmethod
    def run_op_in_workspace(self, base_dir: str,
                            op_name: str, op_args: List[str],
                            monitor: Monitor = Monitor.NONE) -> None:
        pass

    @abstractmethod
    def set_workspace_resource(self, base_dir: str, res_name: str,
                               op_name: str, op_args: List[str],
                               monitor: Monitor = Monitor.NONE) -> None:
        pass

    @abstractmethod
    def delete_workspace_resource(self, base_dir: str, res_name: str) -> None:
        pass

    @abstractmethod
    def write_workspace_resource(self, base_dir: str, res_name: str,
                                 file_path: str, format_name: str = None,
                                 monitor: Monitor = Monitor.NONE) -> None:
        pass

    @abstractmethod
    def plot_workspace_resource(self, base_dir: str, res_name: str,
                                var_name: str = None, file_path: str = None,
                                monitor: Monitor = Monitor.NONE) -> None:
        pass

    @abstractmethod
    def print_workspace_resource(self, base_dir: str, res_name_or_expr: str = None,
                                 monitor: Monitor = Monitor.NONE) -> None:
        pass


class FSWorkspaceManager(WorkspaceManager):
    def __init__(self, resolve_dir: str = None):
        self._open_workspaces = OrderedDict()
        self._resolve_dir = os.path.abspath(resolve_dir or os.curdir)

    def num_open_workspaces(self) -> int:
        return len(self._open_workspaces)

    def resolve_path(self, dir_path):
        if dir_path and os.path.isabs(dir_path):
            return os.path.normpath(dir_path)
        return os.path.abspath(os.path.join(self._resolve_dir, dir_path or ''))

    def get_open_workspaces(self) -> List[Workspace]:
        return list(self._open_workspaces.values())

    def get_workspace(self, base_dir: str, open: bool = False) -> Workspace:
        base_dir = self.resolve_path(base_dir)
        workspace = self._open_workspaces.get(base_dir, None)
        if workspace is not None:
            assert not workspace.is_closed
            return workspace
        if not open:
            raise WorkspaceError('workspace does not exist: ' + base_dir)
        workspace = Workspace.open(base_dir)
        assert base_dir not in self._open_workspaces
        self._open_workspaces[base_dir] = workspace
        return workspace

    def new_workspace(self, base_dir: str, save: bool = False, description: str = None) -> Workspace:
        base_dir = self.resolve_path(base_dir)
        if base_dir in self._open_workspaces:
            raise WorkspaceError('workspace already opened: %s' % base_dir)
        workspace_dir = Workspace.get_workspace_dir(base_dir)
        if os.path.isdir(workspace_dir):
            raise WorkspaceError('workspace exists, consider opening it: %s' % base_dir)
        workspace = Workspace.create(base_dir, description=description)
        assert base_dir not in self._open_workspaces
        if save:
            workspace.save()
        self._open_workspaces[base_dir] = workspace
        return workspace

    def open_workspace(self, base_dir: str) -> Workspace:
        return self.get_workspace(base_dir, open=True)

    def close_workspace(self, base_dir: str, save: bool) -> None:
        base_dir = self.resolve_path(base_dir)
        workspace = self._open_workspaces.pop(base_dir, None)
        if workspace is not None:
            if save and workspace.is_modified:
                workspace.save()
            workspace.close()

    def close_all_workspaces(self, save: bool) -> None:
        workspaces = self._open_workspaces.values()
        self._open_workspaces = dict()
        for workspace in workspaces:
            if save:
                workspace.save()
            workspace.close()

    def save_workspace(self, base_dir: str) -> None:
        base_dir = self.resolve_path(base_dir)
        workspace = self.get_workspace(base_dir)
        if workspace is not None and workspace.is_modified:
            workspace.save()

    def save_all_workspaces(self) -> None:
        workspaces = self._open_workspaces.values()
        self._open_workspaces = dict()
        for workspace in workspaces:
            workspace.save()

    def clean_workspace(self, base_dir: str) -> None:
        base_dir = self.resolve_path(base_dir)
        workflow_file = Workspace.get_workflow_file(base_dir)
        old_workflow = None
        if os.path.isfile(workflow_file):
            # noinspection PyBroadException
            try:
                old_workflow = Workflow.load(workflow_file)
            except:
                pass
            try:
                os.remove(workflow_file)
            except (IOError, OSError) as e:
                raise WorkspaceError(e)
        old_workspace = self._open_workspaces.get(base_dir)
        if old_workspace:
            old_workspace.resource_cache.close()
        # Create new workflow but keep old header info
        workflow = Workspace.new_workflow(header_dict=old_workflow.op_meta_info.header if old_workflow else None)
        workspace = Workspace(base_dir, workflow)
        self._open_workspaces[base_dir] = workspace
        workspace.save()

    def delete_workspace(self, base_dir: str) -> None:
        self.close_workspace(base_dir, save=False)
        base_dir = self.resolve_path(base_dir)
        workspace_dir = Workspace.get_workspace_dir(base_dir)
        if not os.path.isdir(workspace_dir):
            raise WorkspaceError('not a workspace: %s' % base_dir)
        try:
            shutil.rmtree(workspace_dir)
        except (IOError, OSError) as e:
            raise WorkspaceError(e)

    def run_op_in_workspace(self, base_dir: str,
                            op_name: str, op_args: List[str],
                            monitor: Monitor = Monitor.NONE) -> None:
        workspace = self.get_workspace(base_dir)
        workspace.run_op(op_name, op_args, validate_args=True, monitor=monitor)

    def set_workspace_resource(self, base_dir: str, res_name: str, op_name: str, op_args: List[str],
                               monitor: Monitor = Monitor.NONE) -> None:
        workspace = self.get_workspace(base_dir)
        workspace.set_resource(res_name, op_name, op_args, overwrite=True, validate_args=True)
        workspace.execute_workflow(res_name, monitor)

    def delete_workspace_resource(self, base_dir: str, res_name: str) -> None:
        workspace = self.get_workspace(base_dir)
        workspace.delete_resource(res_name)

    def write_workspace_resource(self, base_dir: str, res_name: str,
                                 file_path: str, format_name: str = None,
                                 monitor: Monitor = Monitor.NONE) -> None:
        workspace = self.get_workspace(base_dir)
        with monitor.starting('Writing resource "%s"' % res_name, total_work=10):
            obj = workspace.execute_workflow(res_name, monitor.child(9))
            write_object(obj, file_path, format_name=format_name)
            monitor.progress(work=1, msg='Writing file %s' % file_path)

    def plot_workspace_resource(self, base_dir: str, res_name: str,
                                var_name: str = None, file_path: str = None,
                                monitor: Monitor = Monitor.NONE) -> None:
        workspace = self.get_workspace(base_dir)
        obj = self._get_resource_value(workspace, res_name, monitor)

        import xarray as xr
        import numpy as np
        import matplotlib
        matplotlib.use('Qt5Agg')
        import matplotlib.pyplot as plt

        if isinstance(obj, xr.Dataset):
            ds = obj
            if var_name:
                variables = [ds.data_vars[var_name]]
            else:
                variables = ds.data_vars.values()
            for var in variables:
                if hasattr(var, 'plot'):
                    print('Plotting ', var)
                    var.plot()
            plt.show()
        elif isinstance(obj, xr.DataArray):
            var = obj
            if hasattr(var, 'plot'):
                print('Plotting ', var)
                var.plot()
                plt.show()
        elif isinstance(obj, np.ndarray):
            plt.plot(obj)
            plt.show()
        else:
            raise WorkspaceError("don't know how to plot a \"%s\"" % type(obj))

    def print_workspace_resource(self, base_dir: str, res_name_or_expr: str = None,
                                 monitor: Monitor = Monitor.NONE) -> None:
        workspace = self.get_workspace(base_dir)
        value = self._get_resource_value(workspace, res_name_or_expr, monitor)
        pprint.pprint(value)

    def _get_resource_value(self, workspace, res_name_or_expr, monitor):
        value = UNDEFINED
        if res_name_or_expr is None:
            value = workspace.resource_cache
        elif res_name_or_expr.isidentifier() and workspace.workflow.find_node(res_name_or_expr) is not None:
            value = workspace.execute_workflow(res_name_or_expr, monitor)
        if value is UNDEFINED:
            value = eval(res_name_or_expr, None, workspace.resource_cache)
        return value


class WebAPIWorkspaceManager(WorkspaceManager):
    def __init__(self, service_info: dict, timeout=120):
        address = service_info.get('address', None) or '127.0.0.1'
        port = service_info.get('port', None)
        if not port:
            raise ValueError('missing "port" number in service_info argument')
        self.base_url = 'http://%s:%s' % (address, port)
        self.timeout = timeout

    def _url(self, path_pattern: str, path_args: dict = None, query_args: dict = None) -> str:
        return self.base_url + encode_url_path(path_pattern, path_args=path_args, query_args=query_args)

    def _fetch_json(self, url, data=None, error_type=WorkspaceError, timeout: float = None):
        with urllib.request.urlopen(url, data=data, timeout=timeout or self.timeout) as response:
            json_text = response.read()
        json_response = json.loads(json_text.decode('utf-8'))
        status = json_response.get('status')
        if status == 'error':
            error_details = json_response.get('error')
            message = error_details.get('message') if error_details else None
            type_name = error_details.get('type') if error_details else None
            trace_back = error_details.get('traceback') if error_details else None
            message = message or type_name or ''
            if trace_back:
                message += self.get_traceback_header() + trace_back
            raise error_type(message)
        return json_response.get('content')

    def _query(self, **kwargs: dict):
        return {key: value for key, value in kwargs.items() if value is not None}

    def _post_data(self, **kwargs: dict):
        data = urllib.parse.urlencode(self._query(**kwargs))
        return data.encode() if data else None

    @classmethod
    def get_traceback_header(cls) -> str:
        traceback_title = 'Cate WebAPI service traceback'
        traceback_line = len(traceback_title) * '='
        return '\n' + traceback_line + '\n' + traceback_title + '\n' + traceback_line + '\n'

    def is_running(self, timeout: float = None) -> bool:
        # noinspection PyBroadException
        try:
            self._fetch_json('/', timeout=timeout)
            return True
        except WorkspaceError:
            return True
        except:
            return False

    def get_open_workspaces(self) -> List[Workspace]:
        url = self._url('/ws/get_open')
        json_list = self._fetch_json(url, timeout=WORKSPACE_TIMEOUT)
        return [Workspace.from_json_dict(ws_json_dict) for ws_json_dict in json_list]

    def get_workspace(self, base_dir: str, open: bool = False) -> Workspace:
        url = self._url('/ws/get/{base_dir}',
                        path_args=dict(base_dir=base_dir),
                        query_args=dict(open=open))
        json_dict = self._fetch_json(url, timeout=WORKSPACE_TIMEOUT)
        return Workspace.from_json_dict(json_dict)

    def new_workspace(self, base_dir: str, save: bool = False, description: str = None) -> Workspace:
        url = self._url('/ws/new',
                        query_args=dict(base_dir=base_dir,
                                        save=save,
                                        description=description or ''))
        json_dict = self._fetch_json(url, timeout=WORKSPACE_TIMEOUT)
        return Workspace.from_json_dict(json_dict)

    def open_workspace(self, base_dir: str) -> Workspace:
        url = self._url('/ws/open/{base_dir}',
                        path_args=dict(base_dir=base_dir))
        json_dict = self._fetch_json(url, timeout=WORKSPACE_TIMEOUT)
        return Workspace.from_json_dict(json_dict)

    def close_workspace(self, base_dir: str, save: bool) -> None:
        url = self._url('/ws/close/{base_dir}',
                        path_args=dict(base_dir=base_dir),
                        query_args=self._query(save=save))
        self._fetch_json(url, timeout=WORKSPACE_TIMEOUT)

    def close_all_workspaces(self, save: bool) -> None:
        url = self._url('/ws/close_all',
                        query_args=self._query(save=save))
        self._fetch_json(url, timeout=WORKSPACE_TIMEOUT)

    def save_workspace(self, base_dir: str) -> None:
        url = self._url('/ws/save/{base_dir}',
                        path_args=dict(base_dir=base_dir))
        self._fetch_json(url, timeout=WORKSPACE_TIMEOUT)

    def save_all_workspaces(self) -> None:
        url = self._url('/ws/save_all')
        self._fetch_json(url, timeout=WORKSPACE_TIMEOUT)

    def delete_workspace(self, base_dir: str) -> None:
        url = self._url('/ws/del/{base_dir}',
                        path_args=dict(base_dir=base_dir))
        self._fetch_json(url, timeout=WORKSPACE_TIMEOUT)

    def clean_workspace(self, base_dir: str) -> None:
        url = self._url('/ws/clean/{base_dir}',
                        path_args=dict(base_dir=base_dir))
        self._fetch_json(url, timeout=WORKSPACE_TIMEOUT)

    def run_op_in_workspace(self, base_dir: str,
                            op_name: str, op_args: List[str],
                            monitor: Monitor = Monitor.NONE) -> None:
        url = self._url('/ws/run_op/{base_dir}',
                        path_args=dict(base_dir=base_dir))
        self._fetch_json(url, timeout=RESOURCE_TIMEOUT,
                         data=self._post_data(op_name=op_name, op_args=json.dumps(op_args)))

    def delete_workspace_resource(self, base_dir: str, res_name: str) -> None:
        url = self._url('/ws/res/del/{base_dir}/{res_name}',
                        path_args=dict(base_dir=base_dir, res_name=res_name))
        self._fetch_json(url, timeout=RESOURCE_TIMEOUT)

    def set_workspace_resource(self, base_dir: str, res_name: str,
                               op_name: str, op_args: List[str],
                               monitor: Monitor = Monitor.NONE) -> None:
        url = self._url('/ws/res/set/{base_dir}/{res_name}',
                        path_args=dict(base_dir=base_dir, res_name=res_name))
        self._fetch_json(url, timeout=RESOURCE_TIMEOUT,
                         data=self._post_data(op_name=op_name, op_args=json.dumps(op_args)))

    def write_workspace_resource(self, base_dir: str, res_name: str,
                                 file_path: str, format_name: str = None,
                                 monitor: Monitor = Monitor.NONE) -> None:
        url = self._url('/ws/res/write/{base_dir}/{res_name}',
                        path_args=dict(base_dir=base_dir, res_name=res_name),
                        query_args=self._query(file_path=file_path, format_name=format_name))
        self._fetch_json(url, timeout=RESOURCE_TIMEOUT)

    def plot_workspace_resource(self, base_dir: str, res_name: str,
                                var_name: str = None, file_path: str = None,
                                monitor: Monitor = Monitor.NONE) -> None:
        url = self._url('/ws/res/plot/{base_dir}/{res_name}',
                        path_args=dict(base_dir=base_dir, res_name=res_name),
                        query_args=self._query(var_name=var_name, file_path=file_path))
        self._fetch_json(url, timeout=RESOURCE_TIMEOUT + PLOT_TIMEOUT)

    def print_workspace_resource(self, base_dir: str, res_name_or_expr: str = None,
                                 monitor: Monitor = Monitor.NONE) -> None:
        url = self._url('/ws/res/print/{base_dir}',
                        path_args=dict(base_dir=base_dir),
                        query_args=self._query(res_name_or_expr=res_name_or_expr))
        self._fetch_json(url, timeout=RESOURCE_TIMEOUT)