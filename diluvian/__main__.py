# -*- coding: utf-8 -*-
"""Command line interface for diluvian."""


import argparse
import logging
import os

from .config import CONFIG


def _make_main_parser():
    """Construct the argparse parser for the main CLI.

    This exists as a separate function so the parser can be used to
    auto-generate CLI documentation in Sphinx.

    Returns
    -------
    argparse.ArgumentParser
        Parser for the main CLI and all subcommands.
    """
    common_parser = argparse.ArgumentParser(add_help=False)

    common_parser.add_argument(
            '-c', '--config-file', action='append', dest='config_files', default=[],
            help='Configuration files to use. For defaults, see `conf/default.toml`. '
                 'Values are overwritten in the order provided.')
    common_parser.add_argument(
            '-m', '--model-file', dest='model_file', default=None,
            help='Existing network model file to use for prediction or continued training.')
    common_parser.add_argument(
            '-v', '--volume-file', dest='volume_file', default=None,
            help='Volume configuration file. For example, see `conf/cremi_datasets.toml`.')
    common_parser.add_argument(
            '--no-in-memory', action='store_false', dest='in_memory', default=True,
            help='Do not preload entire volumes into memory.')
    common_parser.add_argument(
            '-l', '--log', dest='log_level',
            choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
            help='Set the logging level.')

    parser = argparse.ArgumentParser(description='Train or run flood-filling networks on EM data.')

    commandparsers = parser.add_subparsers(help='Commands', dest='command')

    train_parser = commandparsers.add_parser(
            'train', parents=[common_parser],
            help='Train a network from labeled volumes.')
    train_parser.add_argument(
            '-mo', '--model-output-filebase', dest='model_output_filebase', default=None,
            help='Base filename for the best trained model and other output artifacts, '
                 'such as metric plots and configuration state.')
    train_parser.add_argument(
            '-mc', '--model-checkpoint-file', dest='model_checkpoint_file', default=None,
            help='Filename for model checkpoints at every epoch. '
                 'This is different that the model output file; if provided, this HDF5 model '
                 'file is saved every epoch regardless of validation performance.'
                 'Can use Keras format arguments: https://keras.io/callbacks/#modelcheckpoint')
    train_parser.add_argument(
            '--no-generator-reset', action='store_false', dest='reset_generators_each_epoch', default=True,
            help='Do not reset training data generators after each epoch. Otherwise an identical '
                 'sequence of subvolumes are used each training epoch.')
    train_parser.add_argument(
            '--tensorboard', action='store_true', dest='tensorboard', default=False,
            help='Output tensorboard log files while training.')
    train_parser.add_argument(
            '--viewer', action='store_true', dest='viewer', default=False,
            help='Create a neuroglancer viewer for a training sample at the end of training.')
    train_parser.add_argument(
            '--metric-plot', action='store_true', dest='metric_plot', default=False,
            help='Plot metric history at the end of training. '
                 'Will be saved as a PNG with the model output base filename.')

    fill_common_parser = argparse.ArgumentParser(add_help=False)
    fill_common_parser.add_argument(
            '--no-bias', action='store_false', dest='bias', default=True,
            help='Overwrite prediction mask at the end of each field of view inference '
                 'rather than using the anti-merge bias update.')
    fill_common_parser.add_argument(
            '--move-batch-size', dest='move_batch_size', default=1, type=int,
            help='Maximum number of fill moves to process in each prediction batch.')

    sparse_fill_parser = commandparsers.add_parser(
            'sparse-fill', parents=[common_parser, fill_common_parser],
            help='Use a trained network to fill random regions in a volume.')
    sparse_fill_parser.add_argument(
            '-bi', '--bounds-input-file', dest='bounds_input_file', default=None,
            help='Filename for bounds CSV input. Should contain "{volume}", which will be '
                 'substituted with the volume name for each respective volume\'s bounds.')
    sparse_fill_parser.add_argument(
            '--max-moves', dest='max_moves', default=None, type=int,
            help='Cancel filling after this many moves.')
    sparse_fill_parser.add_argument(
            '--multi-gpu-model-kludge', dest='multi_gpu_model_kludge', default=None, type=int,
            help='Fix using a multi-GPU trained model that was not saved properly by '
                 'setting this to the number of training GPUs.')

    check_config_parser = commandparsers.add_parser(
            'check-config', parents=[common_parser],
            help='Check a configuration value.')
    check_config_parser.add_argument(
            'config_property', default=None, nargs='?',
            help='Name of the property to show, e.g., `training.batch_size`.')

    gen_subv_bounds_parser = commandparsers.add_parser(
            'gen-subv-bounds', parents=[common_parser],
            help='Generate subvolume bounds.')
    gen_subv_bounds_parser.add_argument(
            'bounds_output_file', default=None,
            help='Filename for the CSV output. Should contain "{volume}", which will be '
                 'substituted with the volume name for each respective volume\'s bounds.')
    gen_subv_bounds_parser.add_argument(
            'num_bounds', default=None, type=int,
            help='Number of bounds to generate.')

    return parser


def main():
    """Entry point for the diluvian command line interface."""
    parser = _make_main_parser()

    args = parser.parse_args()

    if args.log_level:
        logging.basicConfig(level=logging.getLevelName(args.log_level))

    if args.config_files:
        CONFIG.from_toml(*args.config_files)

    if args.command == 'train':
        # Late import to prevent loading large modules for short CLI commands.
        from .diluvian import train_network

        volumes = load_volumes(args.volume_file, args.in_memory)
        train_network(model_file=args.model_file,
                      volumes=volumes,
                      reset_generators_each_epoch=args.reset_generators_each_epoch,
                      model_output_filebase=args.model_output_filebase,
                      model_checkpoint_file=args.model_checkpoint_file,
                      tensorboard=args.tensorboard,
                      viewer=args.viewer,
                      metric_plot=args.metric_plot)

    elif args.command == 'sparse-fill':
        # Late import to prevent loading large modules for short CLI commands.
        from .diluvian import fill_region_from_model

        volumes = load_volumes(args.volume_file, args.in_memory)
        fill_region_from_model(args.model_file,
                               volumes=volumes,
                               bounds_input_file=args.bounds_input_file,
                               bias=args.bias,
                               move_batch_size=args.move_batch_size,
                               max_moves=args.max_moves,
                               multi_gpu_model_kludge=args.multi_gpu_model_kludge)

    elif args.command == 'check-config':
        prop = CONFIG
        if args.config_property is not None:
            properties = args.config_property.split('.')
            for p in properties:
                prop = getattr(prop, p)
        print prop

    elif args.command == 'gen-subv-bounds':
        # Late import to prevent loading large modules for short CLI commands.
        from .diluvian import generate_subvolume_bounds

        volumes = load_volumes(args.volume_file, args.in_memory)
        generate_subvolume_bounds(args.bounds_output_file, volumes, args.num_bounds)


def load_volumes(volume_file, in_memory):
    """Load HDF5 volumes specified in a TOML description file.

    Parameters
    ----------
    volume_file : str
        Filename of the TOML volume description to load.
    in_memory : bool
        If true, the entire dataset is read into an in-memory volume.

    Returns
    -------
    diluvian.volumes.Volume
    """
    # Late import to prevent loading large modules for short CLI commands.
    from .volumes import HDF5Volume

    print 'Loading volumes...'
    if volume_file:
        volumes = HDF5Volume.from_toml(volume_file)
    else:
        volumes = HDF5Volume.from_toml(os.path.join(os.path.dirname(__file__), '..', 'conf', 'cremi_datasets.toml'))

    if in_memory:
        print 'Copying volumes to memory...'
        volumes = {k: v.to_memory_volume() for k, v in volumes.iteritems()}

    print 'Done.'
    return volumes


if __name__ == "__main__":
    main()
