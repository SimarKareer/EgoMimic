import json
import os
import robomimic.utils.obs_utils as ObsUtils
import torch
from pytorch_lightning import Trainer, seed_everything
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.loggers import WandbLogger

import robomimic.utils.obs_utils as ObsUtils
import robomimic.utils.file_utils as FileUtils

from mimicplay.configs import config_factory
from mimicplay.algo import algo_factory
from mimicplay.utils.train_utils import get_exp_dir, load_data_for_training
import copy
from mimicplay.scripts.aloha_process.simarUtils import nds
from mimicplay.pl_utils.pl_model import ModelWrapper
from mimicplay.pl_utils.pl_data_utils import DataModuleWrapper, DualDataModuleWrapper, get_dual_data_module, get_data_module, json_to_config

def init_dataset(config, dataset_path):
    # load basic metadata from training file
    # print("\n============= Loaded Environment Metadata =============")
    # env_meta = FileUtils.get_env_metadata_from_dataset(dataset_path=config.train.data)
    shape_meta = FileUtils.get_shape_metadata_from_dataset(
        dataset_path=dataset_path,
        all_obs_keys=config.all_obs_keys,
        verbose=True,
        ac_key=config.train.ac_key
    )

    # if type(env_meta) is list:
    #     env_metas = env_meta
    # else:
    #     env_metas = [env_meta]
    # create environment

    # load training data
    trainset, validset = load_data_for_training(
        config, obs_keys=shape_meta["all_obs_keys"], dataset_path=dataset_path
    )
    # load training data
    print("\n============= Training Dataset =============")
    print(trainset)
    print("")

    return trainset, validset, shape_meta

def eval(config, ckpt_path):
    resume_dir = os.path.dirname(os.path.dirname(ckpt_path))
    video_dir = os.path.join(resume_dir, "eval_videos")

    dataset_path = os.path.expanduser(config.train.data)
    ObsUtils.initialize_obs_utils_with_config(config)
    trainset, validset, shape_meta = init_dataset(config, dataset_path)


    train_sampler = trainset.get_dataset_sampler()
    valid_sampler = validset.get_dataset_sampler()
    
    datamodule=get_data_module(trainset, validset, train_sampler, valid_sampler, config)
    model = ModelWrapper.load_from_checkpoint(ckpt_path, datamodule=datamodule)
    # model=ModelWrapper(model, datamodule)
    step_log = model.custom_eval(video_dir)
    # write step_log to file

    with open(os.path.join(video_dir, "step_log.txt"), "w") as f:
        for k, v in step_log.items():
            f.write(f"{k}: {v}\n")

    print(step_log)

def train(config, ckpt_path=None):
    """
    Train a model using the algorithm.
    """
    RANK = int(os.environ["SLURM_PROCID"])
    torch.set_float32_matmul_precision("medium")
    seed_everything(config.train.seed, workers=True)

    if ckpt_path is not None:
        resume_dir = os.path.dirname(os.path.dirname(ckpt_path))

        log_dir, ckpt_dir, video_dir = (
            os.path.join(resume_dir, "logs"),
            os.path.join(resume_dir, "models"),
            os.path.join(resume_dir, "videos"),
        )
    else:
        print("\n============= New Training Run with Config =============")
        print(config)
        print("")
        log_dir, ckpt_dir, video_dir, time_str = get_exp_dir(config, rank=RANK)
        base_output_dir = os.path.join(config.train.output_dir, config.experiment.name)
        exp_dir = os.path.join(base_output_dir, time_str)

    # if config.experiment.logging.terminal_output_to_txt:
    #     # log stdout and stderr to a text file
    #     logger = PrintLogger(os.path.join(log_dir, "log.txt"))
    #     sys.stdout = logger
    #     sys.stderr = logger

    # read config to set up metadata for observation modalities (e.g. detecting rgb observations)
    ObsUtils.initialize_obs_utils_with_config(config)

    # make sure the dataset exists
    dataset_path = os.path.expanduser(config.train.data)
    dataset_path_2 = None if config.train.data_2 is None else os.path.expanduser(config.train.data_2)
    if not os.path.exists(dataset_path):
        raise Exception("Dataset at provided path {} not found!".format(dataset_path))
    if dataset_path_2 and not os.path.exists(dataset_path_2):
        raise Exception("Dataset at provided path {} not found!".format(dataset_path_2))

    trainset, validset, shape_meta = init_dataset(config, dataset_path)


    if dataset_path_2:
        config_2 = copy.deepcopy(config)
        # TODO: currently hardcoding the obs key for the second dataset
        config_2.observation.modalities.obs.rgb = config_2.observation_hand.modalities.obs.rgb
        config_2.observation.modalities.obs.low_dim = config_2.observation_hand.modalities.obs.low_dim
        config_2.train.dataset_keys = config_2.train.dataset_keys_hand
        config_2.train.ac_key = config_2.train.ac_key_hand
        config_2.train.seq_length = config_2.train.seq_length_hand
        config_2.train.seq_length_to_load = config_2.train.seq_length_to_load_hand
        trainset_2, validset_2, _ = init_dataset(config_2, dataset_path_2)
    

    # setup for a new training runs
    model = algo_factory(
        algo_name=config.algo_name,
        config=config,
        obs_key_shapes=shape_meta["all_shapes"],
        ac_dim=shape_meta["ac_dim"],
        device="cuda"  # default to cpu, pl will move to gpu
    )

    print("\n============= Model Summary =============")
    print(model)  # print model summary
    print("")

    # if config.train.ckpt_path is not None:
    #     model = ModelWrapper.load_from_checkpoint(config.train.ckpt_path, model=model).model

    # save the config as a json file
    if RANK == 0:
        with open(os.path.join(log_dir, "..", "config.json"), "w") as outfile:
            json.dump(config, outfile, indent=4)


    # maybe retreve statistics for normalizing observations
    obs_normalization_stats = None
    # if config.train.hdf5_normalize_obs:
    #     obs_normalization_stats = trainset.get_obs_normalization_stats()

    loggers = [] if config.experiment.logging.wandb_proj_name is None else [WandbLogger(
        project=config.experiment.logging.wandb_proj_name,
        sync_tensorboard=True,
        name=config.experiment.description,
        config=config,
        save_dir=log_dir,
    )]

    # breakpoint()
    callbacks = [
        ModelCheckpoint(
            every_n_epochs=config.experiment.save.every_n_epochs,
            dirpath=ckpt_dir,
            save_on_train_epoch_end=True,
            filename="model_epoch_{epoch}",
            save_top_k=-1,
        ),
        # ModelCheckpoint(
        #     dirpath=ckpt_dir,
        #     save_on_train_epoch_end=True,
        #     filename="model_epoch_{epoch}_{final_mse_avg:.1f}",
        #     save_top_k=3,
        #     monitor="final_mse_avg",
        #     mode="min",
        # )
    ]
    # if config.train.use_swa:
    #     callbacks.append(
    #         StochasticWeightAveraging(swa_lrs=config.algo.optim_params.policy.learning_rate.initial)
    #     )
    trainer = Trainer(
        max_epochs=config.train.num_epochs,
        limit_train_batches=config.experiment.epoch_every_n_steps,
        accelerator="gpu",
        devices=config.train.gpus_per_node,
        num_nodes=config.train.num_nodes,
        logger=loggers,
        default_root_dir=exp_dir,
        callbacks=callbacks,
        fast_dev_run=config.train.fast_dev_run,
        # val_check_interval=config.experiment.validation_epoch_every_n_steps,
        check_val_every_n_epoch=config.experiment.validation_freq,
        # gradient_clip_algorithm="norm",
        # gradient_clip_val=config.train.max_grad_norm,
        # precision=16 if config.train.amp_enabled else 32,
        precision=32,
        reload_dataloaders_every_n_epochs=0,
        use_distributed_sampler=True,
        # strategy=DDPStrategy(
        #     find_unused_parameters=False,
        #     static_graph=True,
        #     gradient_as_bucket_view=True,
        # ),
        strategy="ddp_find_unused_parameters_true",
        profiler="simple",
        # profiler=AdvancedProfiler(dirpath=".", filename="perf_logs")
        # if args.profiler != "none"
        # else None,
    )

    train_sampler = trainset.get_dataset_sampler()
    valid_sampler = validset.get_dataset_sampler()
    
    if dataset_path_2 is not None:
        datamodule = get_dual_data_module(trainset, trainset_2, validset, validset_2, train_sampler, valid_sampler, config)
    else:
        datamodule = get_data_module(trainset, validset, train_sampler, valid_sampler, config)

    # dict is picklable, so pass that to model, then create robomimic config inside model
    dataset_path = os.path.expanduser(config.train.data)
    shape_meta = FileUtils.get_shape_metadata_from_dataset(
        dataset_path=dataset_path,
        all_obs_keys=config.all_obs_keys,
        verbose=True,
        ac_key=config.train.ac_key
    )
    model=ModelWrapper(config.dump(), shape_meta, datamodule)


    trainer.fit(
        model=model,
        datamodule=datamodule,
        ckpt_path=ckpt_path,
    )