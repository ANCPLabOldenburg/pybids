import difflib
import os.path
from collections import OrderedDict
from functools import partial
from pathlib import Path
from typing import List, Union, Dict

from .utils import BIDSMetadata
from ..exceptions import (
    BIDSEntityError,
    BIDSValidationError,
    NoMatchError,
    TargetError,
)

from ancpbids import CustomOpExpr, EntityExpr, AllExpr, ValidationPlugin, load_dataset, validate_dataset, \
    write_derivative
from ancpbids.query import query, query_entities, FnMatchExpr, AnyExpr
from ancpbids.utils import deepupdate, resolve_segments, convert_to_relative

__all__ = ['BIDSLayoutV2']

from ..utils import natural_sort


class BIDSLayoutMRIMixin:
    def get_tr(self, derivatives=False, **entities):

        """Return the scanning repetition time (TR) for one or more runs.

        Parameters
        ----------
        derivatives : bool
            If True, also checks derivatives images.
        filters : dict
            Optional keywords used to constrain the selected runs.
            Can be any arguments valid for a .get call (e.g., BIDS entities
            or JSON sidecar keys).

        Returns
        -------
        float
            A single float.

        Notes
        -----
        Raises an exception if more than one unique TR is found.
        """
        # Constrain search to functional images
        scope = 'all' if derivatives else 'raw'
        images = self.get(scope=scope, extension=['.nii', '.nii.gz'], suffix='bold', **entities)
        if not images:
            raise NoMatchError("No functional images that match criteria found.")

        all_trs = set()
        for img in images:
            md = img.get_metadata()
            all_trs.add(round(float(md['RepetitionTime']), 5))

        if len(all_trs) > 1:
            raise NoMatchError("Unique TR cannot be found given filters {!r}"
                               .format(entities))
        return all_trs.pop()


class BIDSLayoutV2(BIDSLayoutMRIMixin):
    """A convenience class to provide access to an in-memory representation of a BIDS dataset.

    .. code-block::

        dataset_path = 'path/to/your/dataset'
        layout = BIDSLayout(dataset_path)

    Parameters
    ----------
    ds_dir:
        the (absolute) path to the dataset to load
    """

    def __init__(self, ds_dir: Union[str, Path], validate=True, **kwargs):
        if isinstance(ds_dir, Path):
            ds_dir = ds_dir.absolute()
        self.dataset = load_dataset(ds_dir)
        self.schema = self.dataset.get_schema()
        self.validationReport = None
        if validate:
            self.validationReport = self.validate()
            if self.validationReport.has_errors():
                error_message = os.linesep.join(map(lambda error: error['message'], self.validationReport.get_errors()))
                raise BIDSValidationError(error_message)

    def __getattr__(self, key):
        """Dynamically inspect missing methods for get_<entity>() calls
        and return a partial function of get() if a match is found."""
        if key.startswith('get_'):
            ent_name = key.replace('get_', '')
            ent_name = self.schema.fuzzy_match_entity_key(ent_name)
            return partial(self.get, return_type='id', target=ent_name)
        # Spit out default message if we get this far
        raise AttributeError("%s object has no attribute named %r" %
                             (self.__class__.__name__, key))

    def get_metadata(self, path, include_entities=False, scope='all'):
        """Return metadata found in JSON sidecars for the specified file.

        Parameters
        ----------
        path : str
            Path to the file to get metadata for.
        include_entities : bool, optional
            If True, all available entities extracted
            from the filename (rather than JSON sidecars) are included in
            the returned metadata dictionary.
        scope : str or list, optional
            The scope of the search space. Each element must
            be one of 'all', 'raw', 'self', 'derivatives', or a
            BIDS-Derivatives pipeline name. Defaults to searching all
            available datasets.

        Returns
        -------
        dict
            A dictionary of key/value pairs extracted from all of the
            target file's associated JSON sidecars.

        Notes
        -----
        A dictionary containing metadata extracted from all matching .json
        files is returned. In cases where the same key is found in multiple
        files, the values in files closer to the input filename will take
        precedence, per the inheritance rules in the BIDS specification.

        """
        path = convert_to_relative(self.dataset, path)
        file = self.dataset.get_file(path)
        md = file.get_metadata()
        if md and include_entities:
            schema_entities = {e.entity_: e.literal_ for e in list(self.schema.EntityEnum)}
            md.update({schema_entities[e.key]: e.value for e in file.entities})
        bmd = BIDSMetadata(file.get_absolute_path())
        bmd.update(md)
        return bmd

    def get(self, return_type: str = 'object', target: str = None, scope: str = None,
            extension: Union[str, List[str]] = None, suffix: Union[str, List[str]] = None,
            regex_search=False,
            **entities) -> Union[List[str], List[object]]:
        """Retrieve files and/or metadata from the current Layout.

        Parameters
        ----------
        return_type : str, optional
            Type of result to return. Valid values:
            'object' (default): return a list of matching BIDSFile objects.
            'file' or 'filename': return a list of matching filenames.
            'dir': return a list of directories.
            'id': return a list of unique IDs. Must be used together
                  with a valid target.
        target : str, optional
            Optional name of the target entity to get results for
            (only used if return_type is 'dir' or 'id').
        scope : str or list, optional
            Scope of the search space. If passed, only
            nodes/directories that match the specified scope will be
            searched. Possible values include:
            'all' (default): search all available directories.
            'derivatives': search all derivatives directories.
            'raw': search only BIDS-Raw directories.
            'self': search only the directly called BIDSLayout.
            <PipelineName>: the name of a BIDS-Derivatives pipeline.
        regex_search : bool or None, optional
            Whether to require exact matching
            (False) or regex search (True) when comparing the query string
            to each entity.
        absolute_paths : bool, optional
            Optionally override the instance-wide option
            to report either absolute or relative (to the top of the
            dataset) paths. If None, will fall back on the value specified
            at BIDSLayout initialization.
        invalid_filters (str): Controls behavior when named filters are
            encountered that don't exist in the database (e.g., in the case of
            a typo like subbject='0.1'). Valid values:
                'error' (default): Raise an explicit error.
                'drop': Silently drop invalid filters (equivalent to not having
                    passed them as arguments in the first place).
                'allow': Include the invalid filters in the query, resulting
                    in no results being returned.
        filters : dict
            Any optional key/values to filter the entities on.
            Keys are entity names, values are regexes to filter on. For
            example, passing filters={'subject': 'sub-[12]'} would return
            only files that match the first two subjects. In addition to
            ordinary data types, the following enums are defined (in the
            Query class):
                * Query.NONE: The named entity must not be defined.
                * Query.ANY: the named entity must be defined, but can have any
                    value.

        Returns
        -------
        list of :obj:`bids.layout.BIDSFile` or str
            A list of BIDSFiles (default) or strings (see return_type).
        """
        # Provide some suggestions if target is specified and invalid.
        self_entities = self.get_entities()
        if target is not None and target not in self_entities:
            potential = list(self_entities.keys())
            suggestions = difflib.get_close_matches(target, potential)
            if suggestions:
                message = "Did you mean one of: {}?".format(suggestions)
            else:
                message = "Valid targets are: {}".format(potential)
            raise TargetError(("Unknown target '{}'. " + message)
                              .format(target))
        folder = self.dataset
        result = query(folder, return_type, target, scope, extension, suffix, regex_search, **entities)
        if return_type in 'files':
            result = natural_sort(result)
        return result

    @property
    def entities(self):
        return self.get_entities()

    def get_entities(self, scope: str = None, sort: bool = False) -> dict:
        """Returns a unique set of entities found within the dataset as a dict.
        Each key of the resulting dict contains a list of values (with at least one element).

        Example dict:
        .. code-block::

            {
                'sub': ['01', '02', '03'],
                'task': ['gamblestask']
            }

        Parameters
        ----------
        scope:
            see BIDSLayout.get()
        sort: default is `False`
            whether to sort the keys by name

        Returns
        -------
        dict
            a unique set of entities found within the dataset as a dict
        """
        return query_entities(self.dataset, scope, sort)

    def get_dataset_description(self, scope='self', all_=False) -> Union[List[Dict], Dict]:
        """Return contents of dataset_description.json.

        Parameters
        ----------
        scope : str
            The scope of the search space. Only descriptions of
            BIDSLayouts that match the specified scope will be returned.
            See :obj:`bids.layout.BIDSLayout.get` docstring for valid values.
            Defaults to 'self' --i.e., returns the dataset_description.json
            file for only the directly-called BIDSLayout.
        all_ : bool
            If True, returns a list containing descriptions for
            all matching layouts. If False (default), returns for only the
            first matching layout.

        Returns
        -------
        dict or list of dict
            a dictionary or list of dictionaries (depending on all_).
        """
        all_descriptions = self.dataset.select(self.schema.DatasetDescriptionFile).objects(as_list=True)
        if all_:
            return all_descriptions
        return all_descriptions[0] if all_descriptions else None

    def get_dataset(self) -> object:
        """
        Returns
        -------
            the in-memory representation of this layout/dataset
        """
        return self.dataset

    def add_derivatives(self, path):
        path = convert_to_relative(self.dataset, path)
        self.dataset.create_derivative(path=path)

    def write_derivative(self, derivative):
        """Writes the provided derivative folder to the dataset.
        Note that a 'derivatives' folder will be created if not present.

        Parameters
        ----------
        derivative:
            the derivative folder to write
        """
        assert isinstance(derivative, self.schema.DerivativeFolder)
        write_derivative(self.dataset, derivative)

    def validate(self) -> ValidationPlugin.ValidationReport:
        """Validates a dataset and returns a report object containing any detected validation errors.

        Example
        ----------

        .. code-block::

            report = layout.validate()
            for message in report.messages:
                print(message)
            if report.has_errors():
                raise "The dataset contains validation errors, cannot continue".

        Returns
        -------
            a report object containing any detected validation errors or warning
        """
        return validate_dataset(self.dataset)

    @property
    def files(self):
        return self.get_files()

    def get_files(self, scope='all'):
        """Get BIDSFiles for all layouts in the specified scope.

        Parameters
        ----------
        scope : str
            The scope of the search space. Indicates which
            BIDSLayouts' entities to extract.
            See :obj:`bids.layout.BIDSLayout.get` docstring for valid values.


        Returns:
            A dict, where keys are file paths and values
            are :obj:`bids.layout.BIDSFile` instances.

        """
        all_files = self.get(return_type="object", scope=scope)
        files = {file.get_absolute_path(): file for file in all_files}
        return files

    def get_file(self, filename, scope='all'):
        """Return the BIDSFile object with the specified path.

        Parameters
        ----------
        filename : str
            The path of the file to retrieve. Must be either an absolute path,
            or relative to the root of this BIDSLayout.
        scope : str or list, optional
            Scope of the search space. If passed, only BIDSLayouts that match
            the specified scope will be searched. See :obj:`BIDSLayout.get`
            docstring for valid values. Default is 'all'.

        Returns
        -------
        :obj:`bids.layout.BIDSFile` or None
            File found, or None if no match was found.
        """
        context = self.dataset
        filename = convert_to_relative(self.dataset, filename)
        if scope and scope not in ['all', 'raw', 'self']:
            context, _ = resolve_segments(context, scope)
        return context.get_file(filename)

    @property
    def description(self):
        return self.get_dataset_description()

    @property
    def derivatives(self):
        derivatives = self.dataset.select(self.schema.DerivativeFolder).where(
            CustomOpExpr(lambda df: df.dataset_description is not None)).objects(as_list=True)
        # a dict where the key is the name of the derivative
        return {derivative.name: derivative for derivative in derivatives}

    @property
    def root(self):
        return self.dataset.base_dir_

    def __repr__(self):
        """Provide a tidy summary of key properties."""
        ents = self.get_entities()
        n_subjects = len(set(ents['sub'])) if 'sub' in ents else 0
        n_sessions = len(set(ents['ses'])) if 'ses' in ents else 0
        n_runs = len(set(ents['run'])) if 'run' in ents else 0
        s = ("BIDS Layout: ...{} | Subjects: {} | Sessions: {} | "
             "Runs: {}".format(self.dataset.base_dir_, n_subjects, n_sessions, n_runs))
        return s
