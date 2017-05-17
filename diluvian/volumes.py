# -*- coding: utf-8 -*-
"""Volumes of raw image and labeled object data."""


from collections import namedtuple
import csv
import logging

import h5py
import math
import numpy as np
from PIL import Image
import pytoml as toml
import requests

from keras.utils.data_utils import get_file

from .config import CONFIG
from .octrees import OctreeVolume
from .regions import (
        Region,
        mask_to_output_target,
        )
from .util import pad_dims


DimOrder = namedtuple('DimOrder', ('X', 'Y', 'Z'))


class SubvolumeBounds(object):
    """Sufficient parameters to extract a subvolume from a volume."""
    __slots__ = ('start', 'stop', 'seed', 'label_id',)

    def __init__(self, start=None, stop=None, seed=None, label_id=None):
        assert (start is not None and stop is not None) or seed is not None, "Bounds or seed must be provided"
        self.start = start
        self.stop = stop
        self.seed = seed
        self.label_id = label_id

    @classmethod
    def iterable_from_csv(cls, filename):
        bounds = []
        with open(filename, 'r') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                for k, v in row.iteritems():
                    if not v:
                        row[k] = None
                    elif v[0] == '[':
                        row[k] = np.fromstring(v[1:-1], sep=' ', dtype=np.int64)
                    else:
                        row[k] = int(v)
                bounds.append(cls(**row))

        return bounds

    @classmethod
    def iterable_to_csv(cls, bounds, filename):
        with open(filename, 'w') as csvfile:
            fieldnames = cls.__slots__
            writer = csv.writer(csvfile)
            writer.writerow(fieldnames)
            for bound in bounds:
                writer.writerow([getattr(bound, f) for f in fieldnames])


class Subvolume(object):
    """A subvolume of image data and an optional ground truth object mask."""
    __slots__ = ('image', 'label_mask', 'seed', 'label_id',)

    def __init__(self, image, label_mask, seed, label_id):
        self.image = image
        self.label_mask = label_mask
        self.seed = seed
        self.label_id = label_id

    def f_a(self):
        """Calculate the mask filling fraction of this subvolume.

        Returns
        -------
        float
            Fraction of the subvolume voxels in the object mask.
        """
        return np.count_nonzero(self.label_mask) / float(self.label_mask.size)

    def has_uniform_seed_margin(self, seed_margin=20.0):
        """Test if a subvolume has a margin of uniform label around its seed.

        Parameters
        ----------
        seed_margin : float, optional
            The minimum acceptable margin of uniform target label around the seed
            voxel (in nm, default 20.0).

        Returns
        -------
        bool
            True if the rectangular margin around the seed position is uniform.
        """
        margin = np.ceil(np.reciprocal(np.array(CONFIG.volume.resolution),
                                       dtype='float64') * seed_margin).astype(np.int64)

        mask_target = self.label_mask
        # If data is unlabeled, can not test so always succeed.
        if mask_target is None:
            return True
        ctr = self.seed
        seed_fov = (ctr - margin, ctr + margin + 1)
        seed_region = mask_target[seed_fov[0][0]:seed_fov[1][0],
                                  seed_fov[0][1]:seed_fov[1][1],
                                  seed_fov[0][2]:seed_fov[1][2]]
        return np.all(seed_region)


class SubvolumeGenerator(object):
    """Combines a volume and a subvolume bounds generator into a generator.

    Parameters
    ----------
    volume : Volume
    bounds_generator : SubvolumeBoundsGenerator
    """
    def __init__(self, volume, bounds_generator):
        self.volume = volume
        self.bounds_generator = bounds_generator

    @property
    def shape(self):
        return self.bounds_generator.shape

    def __iter__(self):
        return self

    def reset(self):
        self.bounds_generator.reset()

    def next(self):
        return self.volume.get_subvolume(next(self.bounds_generator))


class MirrorAugmentGenerator(object):
    """Repeats subvolumes from a subvolume generator mirrored along an axis.

    For each subvolume in the original generator, this generator will yield two
    subvolumes: the original subvolume and the subvolume with the image,
    label mask, and seed mirrored along a given axis.

    Parameters
    ----------
    subvolume_generator : SubvolumeGenerator
    axis : int
    """
    def __init__(self, subvolume_generator, axis):
        self.subvolume_generator = subvolume_generator
        self.axis = axis
        self.subvolume = None

    @property
    def shape(self):
        return self.subvolume_generator.shape

    def __iter__(self):
        return self

    def reset(self):
        self.subvolume = None
        self.subvolume_generator.reset()

    def next(self):
        if self.subvolume is None:
            self.subvolume = next(self.subvolume_generator)
            return self.subvolume
        else:
            subv = self.subvolume
            shape = subv.image.shape[self.axis]
            seed = subv.seed.copy()
            seed[self.axis] = shape - subv.seed[self.axis] - 1
            subv = Subvolume(np.flip(subv.image, self.axis),
                             np.flip(subv.label_mask, self.axis),
                             seed,
                             subv.label_id)
            self.subvolume = None
            return subv


class PermuteAxesAugmentGenerator(object):
    """Repeats subvolumes from a subvolume generator with an axes permutation.

    For each subvolume in the original generator, this generator will yield two
    subvolumes: the original subvolume and the subvolume with the image,
    label mask, and seed axes permuted according to a given axes order.

    Parameters
    ----------
    subvolume_generator : SubvolumeGenerator
    axes : sequence of int
    """
    def __init__(self, subvolume_generator, axes):
        self.subvolume_generator = subvolume_generator
        self.axes = list(axes)
        self.subvolume = None

    @property
    def shape(self):
        # This generator actually has two shapes, but not expressed here.
        return self.subvolume_generator.shape

    def __iter__(self):
        return self

    def reset(self):
        self.subvolume = None
        self.subvolume_generator.reset()

    def next(self):
        if self.subvolume is None:
            self.subvolume = next(self.subvolume_generator)
            return self.subvolume
        else:
            subv = self.subvolume
            subv = Subvolume(np.transpose(subv.image, self.axes),
                             np.transpose(subv.label_mask, self.axes),
                             subv.seed[self.axes],
                             self.subvolume.label_id)
            self.subvolume = None
            return subv


class Volume(object):
    DIM = DimOrder(Z=0, Y=1, X=2)

    def __init__(self, resolution, image_data=None, label_data=None, mask_data=None):
        self.resolution = resolution
        self.image_data = image_data
        self.label_data = label_data
        self.mask_data = mask_data
        self._mask_bounds = None

    def local_coord_to_world(self, a):
        return a

    def world_coord_to_local(self, a):
        return a

    def world_mat_to_local(self, m):
        return m

    @property
    def mask_bounds(self):
        return self._mask_bounds

    @property
    def shape(self):
        return tuple(self.world_coord_to_local(np.array(self.image_data.shape)))

    def _get_downsample_from_resolution(self, resolution):
        resolution = np.asarray(resolution)
        downsample = np.log2(np.true_divide(resolution, self.resolution))
        if np.any(downsample < 0):
            raise ValueError('Requested resolution ({}) is higher than volume resolution ({}). '
                             'Upsampling is not supported.'.format(resolution, self.resolution))
        if not np.all(np.equal(np.mod(downsample, 1), 0)):
            raise ValueError('Requested resolution ({}) is not a power-of-2 downsample of '
                             'volume resolution ({}). '
                             'This is currently unsupported.'.format(resolution, self.resolution))
        return downsample.astype(np.int64)

    def downsample(self, resolution):
        downsample = self._get_downsample_from_resolution(resolution)
        if np.all(np.equal(downsample, 0)):
            return self
        return DownsampledVolume(self, downsample)

    def partition(self, partitioning, partition_index):
        if np.array_equal(partitioning, np.ones(3)) and np.array_equal(partition_index, np.zeros(3)):
            return self
        return PartitionedVolume(self, partitioning, partition_index)

    def sparse_wrapper(self, *args):
        return SparseWrappedVolume(self, *args)

    def subvolume_bounds_generator(self, shape=None):
        return self.SubvolumeBoundsGenerator(self, shape)

    def subvolume_generator(self, bounds_generator=None, **kwargs):
        if bounds_generator is None:
            if not kwargs:
                raise ValueError('Bounds generator arguments must be provided if no bounds generator is provided.')
            bounds_generator = self.subvolume_bounds_generator(**kwargs)
        return SubvolumeGenerator(self, bounds_generator)

    def get_subvolume(self, bounds):
        if bounds.start is None or bounds.stop is None:
            raise ValueError('This volume does not support sparse subvolume access.')

        image_subvol = self.image_data[
                bounds.start[0]:bounds.stop[0],
                bounds.start[1]:bounds.stop[1],
                bounds.start[2]:bounds.stop[2]]
        label_subvol = self.label_data[
                bounds.start[0]:bounds.stop[0],
                bounds.start[1]:bounds.stop[1],
                bounds.start[2]:bounds.stop[2]]

        image_subvol = self.world_mat_to_local(image_subvol)
        if np.issubdtype(image_subvol.dtype, np.integer):
            image_subvol = image_subvol.astype('float32') / 256.0

        label_subvol = self.world_mat_to_local(label_subvol)

        seed = bounds.seed
        if seed is None:
            seed = np.array(label_subvol.shape, dtype=np.int64) / 2

        label_id = bounds.label_id
        if label_id is None:
            label_id = label_subvol[tuple(seed)]
        label_mask = label_subvol == label_id

        return Subvolume(image_subvol, label_mask, seed, label_id)

    class SubvolumeBoundsGenerator(object):
        def __init__(self, volume, shape):
            self.volume = volume
            self.shape = shape
            self.margin = np.floor_divide(self.shape, 2).astype(np.int64)
            self.ctr_min = self.margin
            self.ctr_max = (np.array(self.volume.shape) - self.margin - 1).astype(np.int64)
            self.random = np.random.RandomState(CONFIG.random_seed)

            # If the volume has a mask channel, further limit ctr_min and
            # ctr_max to lie inside a margin in the AABB of the mask.
            if self.volume.mask_data is not None:
                mask_min, mask_max = self.volume.mask_bounds

                mask_min = self.volume.local_coord_to_world(mask_min)
                mask_max = self.volume.local_coord_to_world(mask_max)

                self.ctr_min = np.maximum(self.ctr_min, mask_min + self.margin)
                self.ctr_max = np.minimum(self.ctr_max, mask_max - self.margin - 1)

            if np.any(self.ctr_min >= self.ctr_max - self.shape):
                raise ValueError('Cannot generate subvolume bounds: bounds ({}, {}) too small for shape ({})'.format(
                                 np.array_str(self.ctr_min), np.array_str(self.ctr_max), np.array_str(self.shape)))

        def __iter__(self):
            return self

        def reset(self):
            self.random.seed(0)

        def next(self):
            while True:
                ctr = np.array([self.random.randint(self.ctr_min[n], self.ctr_max[n])
                                for n in range(3)]).astype(np.int64)
                start = ctr - self.margin
                stop = ctr + self.margin + np.mod(self.shape, 2).astype(np.int64)

                # If the volume has a mask channel, only accept subvolumes
                # entirely contained in it.
                if self.volume.mask_data is not None:
                    start_local = self.volume.world_coord_to_local(start)
                    stop_local = self.volume.world_coord_to_local(stop)
                    mask = self.volume.mask_data[
                            start_local[0]:stop_local[0],
                            start_local[1]:stop_local[1],
                            start_local[2]:stop_local[2]]
                    if not mask.all():
                        logging.debug('Skipping subvolume not entirely in mask.')
                        continue

                # Only accept subvolumes where the central seed voxel will be
                # of a uniform label after downsampling. For more stringent
                # seed region uniformity filtering, see has_uniform_seed_margin.
                if self.volume.label_data is None:
                    label_id = None
                    break
                seed_min = self.volume.world_coord_to_local(ctr)
                seed_max = self.volume.world_coord_to_local(ctr + 1)
                label_ids = self.volume.label_data[
                        seed_min[0]:seed_max[0],
                        seed_min[1]:seed_max[1],
                        seed_min[2]:seed_max[2]]
                if (label_ids == label_ids.item(0)).all():
                    label_id = label_ids.item(0)
                    break

            return SubvolumeBounds(start, stop, label_id=label_id)


class NdarrayVolume(Volume):
    """A NumPy ndarray-backed volume.

    Since all volumes assume image and label data are ndarray-like, this class
    exists mostly as a bookkeeping convenience to make actual ndarray volumes
    explicit.
    """
    def __init__(self, *args, **kwargs):
        super(NdarrayVolume, self).__init__(*args, **kwargs)
        self.image_data.flags.writeable = False
        self.label_data.flags.writeable = False


class VolumeView(Volume):
    def __init__(self, parent, *args, **kwargs):
        super(VolumeView, self).__init__(*args, **kwargs)
        self.parent = parent

    def parent_coord_to_world(self, a):
        return a

    def local_coord_to_world(self, a):
        return self.parent.local_coord_to_world(self.parent_coord_to_world(a))

    def world_coord_to_parent(self, a):
        return a

    def world_coord_to_local(self, a):
        return self.world_coord_to_parent(self.parent.world_coord_to_local(a))

    def world_mat_to_local(self, m):
        return self.parent.world_mat_to_local(m)

    @property
    def mask_bounds(self):
        return self.parent.mask_bounds

    @property
    def shape(self):
        return self.parent.shape

    def get_subvolume(self, bounds):
        parent_start = self.world_coord_to_parent(bounds.start) if bounds.start is not None else None
        parent_stop = self.world_coord_to_parent(bounds.stop) if bounds.stop is not None else None
        parent_seed = self.world_coord_to_parent(bounds.seed) if bounds.seed is not None else None
        parent_bounds = SubvolumeBounds(start=parent_start,
                                        stop=parent_stop,
                                        seed=parent_seed,
                                        label_id=bounds.label_id)
        return self.parent.get_subvolume(parent_bounds)


class PartitionedVolume(VolumeView):
    """Wrap an existing volume for partitioned access.

    Subvolume accesses to this volume will be offset and clipped to a partition
    of the wrapped volume.

    Parameters
    ----------
    parent : Volume
        The volume to wrap.
    partitioning : iterable of int
        Number of partitions along each axis. Only one axis should be greater
        than 1.
    partition_index : iterable of int
        Index of the partition which this volume will represent.
    """
    def __init__(self, parent, partitioning, partition_index):
        super(PartitionedVolume, self).__init__(
                parent,
                parent.resolution,
                image_data=parent.image_data,
                label_data=parent.label_data,
                mask_data=parent.mask_data)
        self.partitioning = np.asarray(partitioning)
        self.partition_index = np.asarray(partition_index)
        partition_shape = np.floor_divide(np.array(self.parent.shape), self.partitioning)
        self.bounds = ((np.multiply(partition_shape, self.partition_index)).astype(np.int64),
                       (np.multiply(partition_shape, self.partition_index + 1)).astype(np.int64))

    def parent_coord_to_world(self, a):
        return a - self.bounds[0]

    def world_coord_to_parent(self, a):
        return a + self.bounds[0]

    @property
    def mask_bounds(self):
        if self.parent.mask_bounds is None:
            return None
        else:
            bound_min = np.maximum(self.parent.mask_bounds[0], self.bounds[0])
            bound_max = np.minimum(self.parent.mask_bounds[1], self.bounds[1])
            return bound_min, bound_max

    @property
    def shape(self):
        return tuple(self.bounds[1] - self.bounds[0])


class DownsampledVolume(VolumeView):
    """Wrap an existing volume for downsampled access.

    Subvolume accesses to this volume will be downsampled, but continue to use
    the wrapped volume and its data at the original resolution.

    Parameters
    ----------
    parent : Volume
        The volume to wrap.
    downsample : iterable of int
        Integral zoom levels to downsample the wrapped volume.
    """
    def __init__(self, parent, downsample):
        self.scale = np.exp2(downsample).astype(np.int64)
        super(DownsampledVolume, self).__init__(
                parent,
                np.multiply(parent.resolution, self.scale),
                image_data=parent.image_data,
                label_data=parent.label_data,
                mask_data=parent.mask_data)

    def parent_coord_to_world(self, a):
        return np.divide(a, self.scale)

    def world_coord_to_parent(self, a):
        return np.multiply(a, self.scale)

    @property
    def shape(self):
        return tuple(np.floor_divide(np.array(self.parent.shape), self.scale))

    def get_subvolume(self, bounds):
        subvol_shape = bounds.stop - bounds.start
        parent_bounds = SubvolumeBounds(self.world_coord_to_parent(bounds.start),
                                        self.world_coord_to_parent(bounds.stop))
        subvol = self.parent.get_subvolume(parent_bounds)
        subvol.image = subvol.image.reshape(
                [subvol_shape[0], self.scale[0],
                 subvol_shape[1], self.scale[1],
                 subvol_shape[2], self.scale[2]]).mean(5).mean(3).mean(1)
        # Downsample body mask by considering blocks where the majority
        # of voxels are in the body to be in the body. Alternatives are:
        # - Conjunction (tends to introduce false splits)
        # - Disjunction (tends to overdilate and merge)
        # - Mode label (computationally expensive)
        subvol.label_mask = subvol.label_mask.reshape(
                [subvol_shape[0], self.scale[0],
                 subvol_shape[1], self.scale[1],
                 subvol_shape[2], self.scale[2]]).mean(5).mean(3).mean(1) > 0.5

        # Note that this is not a coordinate xform to parent in the typical
        # sense, just a rescaling of the coordinate in the subvolume-local
        # coordinates. Hence no similar call in VolumeView.get_subvolume.
        subvol.seed = self.parent_coord_to_world(subvol.seed)

        return subvol


class SparseWrappedVolume(VolumeView):
    """Wrap a existing volume for memory cached block sparse access."""
    def __init__(self, parent, image_leaf_shape=None, label_leaf_shape=None):
        if image_leaf_shape is None:
            image_leaf_shape = list(CONFIG.model.input_fov_shape)
        if label_leaf_shape is None:
            label_leaf_shape = list(CONFIG.model.input_fov_shape)

        image_data = OctreeVolume(image_leaf_shape,
                                  (np.zeros(3), parent.image_data.shape),
                                  parent.image_data.dtype,
                                  populator=self.image_populator)
        label_data = OctreeVolume(label_leaf_shape,
                                  (np.zeros(3), parent.label_data.shape),
                                  parent.label_data.dtype,
                                  populator=self.label_populator)

        super(SparseWrappedVolume, self).__init__(
                parent,
                parent.resolution,
                image_data=image_data,
                label_data=label_data)

    def image_populator(self, bounds):
        return self.parent.image_data[
                bounds[0][0]:bounds[1][0],
                bounds[0][1]:bounds[1][1],
                bounds[0][2]:bounds[1][2]]

    def label_populator(self, bounds):
        return self.parent.label_data[
                bounds[0][0]:bounds[1][0],
                bounds[0][1]:bounds[1][1],
                bounds[0][2]:bounds[1][2]]


class HDF5Volume(Volume):
    """A volume backed by data views to HDF5 file arrays.

    Parameters
    ----------
    orig_file : str
        Filename of the HDF5 file to load.
    image_dataaset : str
        Full dataset path including groups to the raw image data array.
    label_dataset : str
        Full dataset path including groups to the object label data array.
    """
    @staticmethod
    def from_toml(filename):
        volumes = {}
        with open(filename, 'rb') as fin:
            datasets = toml.load(fin).get('dataset', [])
            for dataset in datasets:
                hdf5_file = dataset['hdf5_file']
                if dataset.get('use_keras_cache', False):
                    hdf5_file = get_file(hdf5_file, dataset['download_url'], md5_hash=dataset.get('download_md5', None))
                image_dataset = dataset.get('image_dataset', None)
                label_dataset = dataset.get('label_dataset', None)
                mask_dataset = dataset.get('mask_dataset', None)
                mask_bounds = dataset.get('mask_bounds', None)
                resolution = dataset.get('resolution', None)
                volume = HDF5Volume(hdf5_file,
                                    image_dataset,
                                    label_dataset,
                                    mask_dataset,
                                    mask_bounds=mask_bounds)
                # If the volume configuration specifies an explicit resolution,
                # override any provided in the HDF5 itself.
                if resolution:
                    logging.info('Overriding resolution for volume "%s"', dataset['name'])
                    volume.resolution = np.array(resolution)
                volumes[dataset['name']] = volume

        return volumes

    @staticmethod
    def write_file(filename, resolution, **kwargs):
        h5file = h5py.File(filename, 'w')
        config = {'hdf5_file': filename}
        channels = ['image', 'label', 'mask']
        default_datasets = {
            'image': 'volumes/raw',
            'label': 'volumes/labels/neuron_ids',
            'mask': 'volumes/labels/mask',
        }
        for channel in channels:
            data = kwargs.get('{}_data'.format(channel), None)
            dataset_name = kwargs.get('{}_dataset'.format(channel), default_datasets[channel])
            if data is not None:
                dataset = h5file.create_dataset(dataset_name, data=data, dtype=data.dtype)
                dataset.attrs['resolution'] = resolution
                config['{}_dataset'.format(channel)] = dataset_name

        h5file.close()

        return config

    def __init__(self, orig_file, image_dataset, label_dataset, mask_dataset, mask_bounds=None):
        logging.debug('Loading HDF5 file "{}"'.format(orig_file))
        self.file = h5py.File(orig_file, 'r')
        self.resolution = None
        self._mask_bounds = tuple(map(np.asarray, mask_bounds)) if mask_bounds is not None else None

        if image_dataset is None and label_dataset is None:
            raise ValueError('HDF5 volume must have either an image or label dataset: {}'.format(orig_file))

        if image_dataset is not None:
            self.image_data = self.file[image_dataset]
            if 'resolution' in self.file[image_dataset].attrs:
                self.resolution = np.array(self.file[image_dataset].attrs['resolution'])

        if label_dataset is not None:
            self.label_data = self.file[label_dataset]
            if 'resolution' in self.file[label_dataset].attrs:
                resolution = np.array(self.file[label_dataset].attrs['resolution'])
                if self.resolution is not None and not np.array_equal(self.resolution, resolution):
                    logging.warning('HDF5 image and label dataset resolutions differ in %s: %s, %s',
                                    orig_file, self.resolution, resolution)
                else:
                    self.resolution = resolution
        else:
            self.label_data = np.full_like(self.image_data, np.NaN, dtype=np.uint64)

        if mask_dataset is not None:
            self.mask_data = self.file[mask_dataset]
        else:
            self.mask_data = None

        if image_dataset is None:
            self.image_data = np.full_like(self.label_data, np.NaN, dtype=np.float32)

        if self.resolution is None:
            self.resolution = np.ones(3)

    @property
    def mask_bounds(self):
        if self._mask_bounds is not None:
            return self._mask_bounds
        if self.mask_data is None:
            return None

        # Explicitly copy the channel to memory. 3x speedup for np ops.
        mask_data = self.mask_data[:]
        mask_min = []
        mask_max = []

        for axes in [(1, 2), (0, 2), (0, 1)]:
            proj = np.any(mask_data, axis=axes)
            amin, amax = np.where(proj)[0][[0, -1]]

            mask_min.append(amin)
            mask_max.append(amax)

        mask_min = np.array(mask_min, dtype=np.int64)
        mask_max = np.array(mask_max, dtype=np.int64)

        self._mask_bounds = (mask_min, mask_max)

        return self._mask_bounds

    def to_memory_volume(self):
        data = ['image_data', 'label_data', 'mask_data']
        data = {
                k: self.world_mat_to_local(getattr(self, k)[:])
                for k in data if getattr(self, k) is not None}
        return NdarrayVolume(self.world_coord_to_local(self.resolution), **data)


class ImageStackVolume(Volume):
    """A volume for block sparse access to image pyramids over HTTP.

    Parameters
    ----------
    bounds : iterable of int
        Shape of the stack at zoom level 0 in pixels.
    resolution : iterable of float
        Resolution of the stack at zoom level 0 in nm.
    tile_width, tile_height : int
        Size of tiles in pixels
    format_url : str
        Format string for building tile URLs from tile parameters.
    zoom_level : int, optional
        Zoom level to use for this volume.
    missing_z : iterable of int, optional
        Voxel z-indices where data is not available.
    image_leaf_shape : tuple of int or ndarray, optional
        Shape of image octree leaves in voxels. Defaults to 10 stacked tiles.
    label_leaf_shape : tuple of int or ndarray, optional
        Shape of label octree leaves in voxels. Defaults to FFN model FOV.
    """
    @staticmethod
    def from_catmaid_stack(stack_info, tile_source_parameters):
        # See https://catmaid.readthedocs.io/en/stable/tile_sources.html
        format_url = {
            1: '{source_base_url}{{z}}/{{row}}_{{col}}_{{zoom_level}}.{file_extension}',
            4: '{source_base_url}{{z}}/{{zoom_level}}/{{row}}_{{col}}.{file_extension}',
            5: '{source_base_url}{{zoom_level}}/{{z}}/{{row}}/{{col}}.{file_extension}',
            7: '{source_base_url}largeDataTileSource/{tile_width}/{tile_height}/'
               '{{zoom_level}}/{{z}}/{{row}}/{{col}}.{file_extension}',
            9: '{source_base_url}{{z}}/{{row}}_{{col}}_{{zoom_level}}.{file_extension}',
        }[tile_source_parameters['tile_source_type']].format(**tile_source_parameters)
        bounds = np.flipud(np.array(stack_info['bounds'], dtype=np.int64))
        resolution = np.flipud(np.array(stack_info['resolution']))
        tile_width = int(tile_source_parameters['tile_width'])
        tile_height = int(tile_source_parameters['tile_height'])
        return ImageStackVolume(bounds, resolution, tile_width, tile_height, format_url,
                                missing_z=stack_info['broken_slices'])

    def __init__(self, bounds, orig_resolution, tile_width, tile_height, tile_format_url,
                 zoom_level=0, missing_z=None, image_leaf_shape=None):
        self.orig_bounds = bounds
        self.orig_resolution = orig_resolution
        self.tile_width = tile_width
        self.tile_height = tile_height
        self.tile_format_url = tile_format_url

        self.zoom_level = int(zoom_level)
        if missing_z is None:
            missing_z = []
        self.missing_z = frozenset(missing_z)
        if image_leaf_shape is None:
            image_leaf_shape = [10, tile_height, tile_width]

        scale = np.exp2(np.array([0, self.zoom_level, self.zoom_level])).astype(np.int64)

        data_shape = (np.zeros(3), np.divide(bounds, scale).astype(np.int64))
        self.image_data = OctreeVolume(image_leaf_shape,
                                       data_shape,
                                       'float32',
                                       populator=self.image_populator)

        self.label_data = None

    @property
    def resolution(self):
        return self.orig_resolution * np.exp2([0, self.zoom_level, self.zoom_level])

    def downsample(self, resolution):
        downsample = self._get_downsample_from_resolution(resolution)
        zoom_level = np.min(downsample[[self.DIM.X, self.DIM.Y]])
        if zoom_level > 0:
            return ImageStackVolume(
                    self.orig_bounds,
                    self.orig_resolution,
                    self.tile_width,
                    self.tile_height,
                    self.tile_format_url,
                    zoom_level=self.zoom_level + zoom_level,
                    missing_z=self.missing_z,
                    image_leaf_shape=self.image_data.leaf_shape).downsample(resolution)
        if np.all(np.equal(downsample, 0)):
            return self
        return DownsampledVolume(self, downsample)

    def subvolume_bounds_generator(self, sparse_margin=None, **kwargs):
        if sparse_margin is not None:
            if kwargs:
                raise ValueError('sparse_margin can not be combined with other arguments.')
            return self.SparseSubvolumeBoundsGenerator(self, sparse_margin)
        return super(ImageStackVolume, self).subvolume_bounds_generator(**kwargs)

    def get_subvolume(self, bounds):
        if bounds.start is None or bounds.stop is None:
            image_subvol = self.image_data
            label_subvol = self.label_data
        else:
            image_subvol = self.image_data[
                    bounds.start[0]:bounds.stop[0],
                    bounds.start[1]:bounds.stop[1],
                    bounds.start[2]:bounds.stop[2]]
            label_subvol = None

        if np.issubdtype(image_subvol.dtype, np.integer):
            raise ValueError('Sparse volume access does not support image data coercion.')

        seed = bounds.seed
        if seed is None:
            seed = np.array(image_subvol.shape, dtype=np.int64) / 2

        return Subvolume(image_subvol, label_subvol, seed, bounds.label_id)

    def image_populator(self, bounds):
        image_subvol = np.zeros(tuple(bounds[1] - bounds[0]), dtype='float32')
        col_range = map(int, (math.floor(bounds[0][self.DIM.X]/self.tile_width),
                              math.ceil(bounds[1][self.DIM.X]/self.tile_width)))
        row_range = map(int, (math.floor(bounds[0][self.DIM.Y]/self.tile_height),
                              math.ceil(bounds[1][self.DIM.Y]/self.tile_height)))
        tile_size = np.array([1, self.tile_height, self.tile_width]).astype('int64')
        for z in xrange(bounds[0][self.DIM.Z], bounds[1][self.DIM.Z]):
            if z in self.missing_z:
                image_subvol[int(z - bounds[0][self.DIM.Z]), :, :] = 0
                continue
            for r in xrange(*row_range):
                for c in xrange(*col_range):
                    url = self.tile_format_url.format(zoom_level=self.zoom_level, z=z, row=r, col=c)
                    try:
                        im = np.array(Image.open(requests.get(url, stream=True).raw))
                        # If the image is multichannel, throw our hands up and
                        # just use the first channel.
                        if im.ndim > 2:
                            im = im[:, :, 0].squeeze()
                        im = im / 256.0
                    except IOError:
                        logging.debug('Failed to load tile: %s', url)
                        im = np.full((self.tile_height, self.tile_width), 0, dtype='float32')
                    tile_coord = np.array([z, r, c]).astype('int64')
                    tile_loc = np.multiply(tile_coord, tile_size)

                    subvol = (np.maximum(np.zeros(3), tile_loc - bounds[0]).astype(np.int64),
                              np.minimum(np.array(image_subvol.shape),
                                         tile_loc + tile_size - bounds[0]).astype(np.int64))
                    tile_sub = (np.maximum(np.zeros(3), bounds[0] - tile_loc).astype(np.int64),
                                np.minimum(tile_size, bounds[1] - tile_loc).astype(np.int64))

                    image_subvol[subvol[0][self.DIM.Z],
                                 subvol[0][self.DIM.Y]:subvol[1][self.DIM.Y],
                                 subvol[0][self.DIM.X]:subvol[1][self.DIM.X]] = \
                        im[tile_sub[0][self.DIM.Y]:tile_sub[1][self.DIM.Y],
                           tile_sub[0][self.DIM.X]:tile_sub[1][self.DIM.X]]

        return image_subvol

    class SparseSubvolumeBoundsGenerator(object):
        def __init__(self, volume, margin):
            self.volume = volume
            self.margin = np.asarray(margin).astype(np.int64)
            self.ctr_min = self.margin
            self.ctr_max = (np.array(self.volume.shape) - self.margin - 1).astype(np.int64)
            self.random = np.random.RandomState(CONFIG.random_seed)

        @property
        def shape(self):
            return self.volume.shape

        def __iter__(self):
            return self

        def reset(self):
            self.random.seed(0)

        def next(self):
            ctr = np.array([self.random.randint(self.ctr_min[n], self.ctr_max[n])
                            for n in range(3)]).astype(np.int64)
            return SubvolumeBounds(seed=ctr)


def static_training_generator(subvolumes, batch_size, training_size,
                              f_a_bins=None, reset_generators=True):
    """Generate Keras non-moving training tuples from a subvolume generator.

    Note that this generator is not yet compatible with networks with different
    input and output FOV shapes.

    Parameters
    ----------
    subvolumes : generator of Subvolume
    batch_size : int
    training_size : int
        Total size in samples of a training epoch, after which generators will
        be reset if ``reset_generators`` is true.
    f_a_bins : sequence of float, optional
        Bin boundaries for filling fractions. If provided, sample loss will be
        weighted to increase loss contribution from less-frequent f_a bins.
        Otherwise all samples are weighted equally.
    reset_generators : bool
        Whether to reset subvolume generators at the end of each epoch. If true
        subvolumes will be sampled in the same order each epoch.
    """
    mask_input = np.full(np.append(subvolumes.shape, (1,)), CONFIG.model.v_false, dtype='float32')
    mask_input[tuple(np.array(mask_input.shape) / 2)] = CONFIG.model.v_true
    mask_input = np.tile(mask_input, (batch_size, 1, 1, 1, 1))
    f_a_init = False

    if f_a_bins is not None:
        f_a_init = True
        f_a_counts = np.ones_like(f_a_bins, dtype=np.int64)
    f_as = np.zeros(batch_size)

    sample_num = 0
    while True:
        if sample_num >= training_size:
            f_a_init = False
            if reset_generators:
                subvolumes.reset()
            sample_num = 0

        batch_image_input = [None] * batch_size
        batch_mask_target = [None] * batch_size

        for batch_ind in range(0, batch_size):
            subvolume = subvolumes.next()

            f_as[batch_ind] = subvolume.f_a()
            batch_image_input[batch_ind] = pad_dims(subvolume.image)
            batch_mask_target[batch_ind] = pad_dims(mask_to_output_target(subvolume.label_mask))

        batch_image_input = np.concatenate(batch_image_input)
        batch_mask_target = np.concatenate(batch_mask_target)

        sample_num += batch_size

        if f_a_bins is None:
            yield ({'image_input': batch_image_input,
                    'mask_input': mask_input},
                   [batch_mask_target])
        else:
            f_a_inds = np.digitize(f_as, f_a_bins) - 1
            inds, counts = np.unique(f_a_inds, return_counts=True)
            if f_a_init:
                f_a_counts[inds] += counts.astype(np.int64)
                sample_weights = np.ones(f_as.size, dtype='float64')
            else:
                sample_weights = np.reciprocal(f_a_counts[f_a_inds], dtype='float64') * float(f_as.size)
            yield ({'image_input': batch_image_input,
                    'mask_input': mask_input},
                   [batch_mask_target],
                   sample_weights)


def moving_training_generator(subvolumes, batch_size, training_size, callback_kludge,
                              f_a_bins=None, reset_generators=True):
    """Generate Keras moving FOV training tuples from a subvolume generator.

    Unlike ``static_training_generator``, this generator expects a subvolume
    generator that will provide subvolumes larger than the network FOV, and
    will allow the output of training at one batch to generate moves within
    these subvolumes to produce training data for the subsequent batch.

    Parameters
    ----------
    subvolumes : generator of Subvolume
    batch_size : int
    training_size : int
        Total size in samples of a training epoch, after which generators will
        be reset if ``reset_generators`` is true.
    callback_kludge : dict
        A kludge object to allow this generator to provide inputs and receive
        outputs from the network. See ``diluvian.PredictionCopy``.
    f_a_bins : sequence of float, optional
        Bin boundaries for filling fractions. If provided, sample loss will be
        weighted to increase loss contribution from less-frequent f_a bins.
        Otherwise all samples are weighted equally.
    reset_generators : bool
        Whether to reset subvolume generators at the end of each epoch. If true
        subvolumes will be sampled in the same order each epoch.
    """
    regions = [None] * batch_size
    region_pos = [None] * batch_size
    move_counts = [0] * batch_size
    epoch_move_counts = []
    batch_image_input = [None] * batch_size
    f_a_init = False

    if f_a_bins is not None:
        f_a_init = True
        f_a_counts = np.ones_like(f_a_bins, dtype=np.int64)
    f_as = np.zeros(batch_size)

    sample_num = 0
    while True:
        if sample_num >= training_size:
            f_a_init = False
            if reset_generators:
                subvolumes.reset()
            if len(epoch_move_counts):
                logging.info(' Average moves: %s', sum(epoch_move_counts)/float(len(epoch_move_counts)))
            epoch_move_counts = []
            sample_num = 0

        # Before clearing last batches, reuse them to predict mask outputs
        # for move training. Add mask outputs to regions.
        active_regions = [n for n, region in enumerate(regions) if region is not None]
        if active_regions and callback_kludge['outputs'] is not None:
            for n in active_regions:
                assert np.array_equal(callback_kludge['inputs']['image_input'][n, 0, 0, :, 0],
                                      batch_image_input[n, 0, 0, :, 0])
                regions[n].add_mask(callback_kludge['outputs'][n, :, :, :, 0], region_pos[n])

        batch_image_input = [None] * batch_size
        batch_mask_input = [None] * batch_size
        batch_mask_target = [None] * batch_size

        for r, region in enumerate(regions):
            if region is None or region.queue.empty():
                subvolume = subvolumes.next()

                regions[r] = Region.from_subvolume(subvolume)
                region = regions[r]
                epoch_move_counts.append(move_counts[r])
                move_counts[r] = 0
            else:
                move_counts[r] += 1

            block_data = region.get_next_block()

            f_as[r] = subvolume.f_a()
            batch_image_input[r] = pad_dims(block_data['image'])
            batch_mask_input[r] = pad_dims(block_data['mask'])
            batch_mask_target[r] = pad_dims(block_data['target'])
            region_pos[r] = block_data['position']

        batch_image_input = np.concatenate(batch_image_input)
        batch_mask_input = np.concatenate(batch_mask_input)
        batch_mask_target = np.concatenate(batch_mask_target)

        sample_num += batch_size
        inputs = {'image_input': batch_image_input,
                  'mask_input': batch_mask_input}
        callback_kludge['inputs'] = inputs
        callback_kludge['outputs'] = None

        if f_a_bins is None:
            yield (inputs,
                   [batch_mask_target])
        else:
            f_a_inds = np.digitize(f_as, f_a_bins) - 1
            inds, counts = np.unique(f_a_inds, return_counts=True)
            if f_a_init:
                f_a_counts[inds] += counts.astype(np.int64)
                sample_weights = np.ones(f_as.size, dtype='float64')
            else:
                sample_weights = np.reciprocal(f_a_counts[f_a_inds], dtype='float64') * float(f_as.size)
            yield (inputs,
                   [batch_mask_target],
                   sample_weights)
