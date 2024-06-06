"""
Implementation of Action Chunking with Transformers (ACT).
"""
from collections import OrderedDict

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as transforms

import robomimic.utils.tensor_utils as TensorUtils
from mimicplay.algo import register_algo_factory_func, PolicyAlgo
from robomimic.algo.bc import BC_VAE
from act.detr.main import build_ACT_model_and_optimizer, build_single_policy_model_and_optimizer
from mimicplay.scripts.aloha_process.simarUtils import nds
import matplotlib.pyplot as plt

@register_algo_factory_func("act")
def algo_config_to_class(algo_config):
    """
    Maps algo config to the BC algo class to instantiate, along with additional algo kwargs.

    Args:
        algo_config (Config instance): algo config

    Returns:
        algo_class: subclass of Algo
        algo_kwargs (dict): dictionary of additional kwargs to pass to algorithm
    """
    algo_class, algo_kwargs = ACT, {}

    return algo_class, algo_kwargs

@register_algo_factory_func("actSP")
def algo_config_to_class(algo_config):
    """
    Maps algo config to the BC algo class to instantiate, along with additional algo kwargs.

    Args:
        algo_config (Config instance): algo config

    Returns:
        algo_class: subclass of Algo
        algo_kwargs (dict): dictionary of additional kwargs to pass to algorithm
    """
    algo_class, algo_kwargs = ACTSP, {}
    return algo_class, algo_kwargs    


class ACT(BC_VAE):
    """
    BC training with a VAE policy.
    """
    def build_model_opt(self, policy_config):
        """
        Builds networks and optimizers for BC algo.
        """
        return build_ACT_model_and_optimizer(policy_config)

    def _create_networks(self):
        """
        Creates networks and places them into @self.nets.
        """

        self.normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])

        self.nets = nn.ModuleDict()
        self.chunk_size = self.global_config["train"]["seq_length"]
        self.camera_keys = self.obs_config['modalities']['obs']['rgb'].copy()
        self.proprio_keys = self.obs_config['modalities']['obs']['low_dim'].copy()
        self.obs_keys = self.proprio_keys + self.camera_keys

        self.proprio_dim = 0
        for k in self.proprio_keys:
            self.proprio_dim += self.obs_key_shapes[k][0]

        policy_config = {
                         'num_queries': self.global_config.train.seq_length,
                         'hidden_dim': self.algo_config.act.hidden_dim,
                         'dim_feedforward': self.algo_config.act.dim_feedforward,
                         'backbone': self.algo_config.act.backbone,
                         'enc_layers': self.algo_config.act.enc_layers,
                         'dec_layers': self.algo_config.act.dec_layers,
                         'nheads': self.algo_config.act.nheads,
                         'latent_dim': self.algo_config.act.latent_dim,
                         'a_dim': self.ac_dim,
                         'ac_key': self.ac_key,
                         'state_dim': self.proprio_dim,
                         'camera_names': self.camera_keys
                         }
        self.kl_weight = self.algo_config.act.kl_weight
        model, optimizer = self.build_model_opt(policy_config)
        self.nets["policy"] = model
        self.nets = self.nets.float().to(self.device)

        self.temporal_agg = False
        self.query_frequency = self.chunk_size  # TODO maybe tune

        self._step_counter = 0
        self.a_hat_store = None

        rand_kwargs = self.global_config.observation.encoder.rgb.obs_randomizer_kwargs
        brightness = 0.0 if "brightness" not in rand_kwargs else rand_kwargs.brightness
        contrast = 0.0 if "contrast" not in rand_kwargs else rand_kwargs.contrast
        saturation = 0.0 if "saturation" not in rand_kwargs else rand_kwargs.saturation
        hue = 0.0 if "hue" not in rand_kwargs else rand_kwargs.hue
        self.color_jitter = transforms.ColorJitter(brightness=brightness, contrast=contrast, saturation=saturation, hue=hue)


    def process_batch_for_training(self, batch):
        assert False, "Must pass in ac_key for this class"


    def process_batch_for_training(self, batch, ac_key):
        """
        Processes input batch from a data loader to filter out
        relevant information and prepare the batch for training.
        Args:
            batch (dict): dictionary with torch.Tensors sampled
                from a data loader
        Returns:
            input_batch (dict): processed and filtered batch that
                will be used for training
        """

        input_batch = dict()
        input_batch["obs"] = {k: batch["obs"][k][:, 0, :] for k in batch["obs"] if k != 'pad_mask' and k != 'type'}
        input_batch["obs"]['pad_mask'] = batch["obs"]['pad_mask']
        input_batch["goal_obs"] = batch.get("goal_obs", None) # goals may not be present
        if ac_key in batch:
            input_batch["actions"] = batch[ac_key]

        input_batch["type"] = batch["type"]
        
        # we move to device first before float conversion because image observation modalities will be uint8 -
        # this minimizes the amount of data transferred to GPU
        return TensorUtils.to_float(TensorUtils.to_device(input_batch, self.device))

    def train_on_batch(self, batch, epoch, validate=False):
        """
        Update from superclass to set categorical temperature, for categorcal VAEs.
        """

        return super(BC_VAE, self).train_on_batch(batch, epoch, validate=validate)

    def _modality_check(self, batch):
        """
        Helper function to check if the batch is robot or hand data.
        """
        if (batch["type"] == 0).all():
            modality = "robot"
        elif (batch["type"] == 1).all():
            modality = "hand"
        else:
            raise ValueError("Got mixed modalities, current implementation expects either robot or hand data only.")
        return modality

    def _robomimic_to_act_data(self, batch, cam_keys, proprio_keys):
        proprio = [batch["obs"][k] for k in proprio_keys]
        proprio = torch.cat(proprio, axis=1)
        qpos = proprio

        images = []
        for cam_name in cam_keys:
            image = batch['obs'][cam_name]
            image = self.color_jitter(image)
            image = self.normalize(image)
            image = image.unsqueeze(axis=1)
            images.append(image)
        images = torch.cat(images, axis=1)

        env_state = torch.zeros([qpos.shape[0], 10]).cuda()  # this is not used

        actions = batch['actions']
        is_pad = batch['obs']['pad_mask'] == 0  # from 1.0 or 0 to False and True
        is_pad = is_pad.squeeze(dim=-1)
        B, T = is_pad.shape
        assert T == self.chunk_size

        return qpos, images, env_state, actions, is_pad


    def _forward_training(self, batch):
        """
        Internal helper function for BC algo class. Compute forward pass
        and return network outputs in @predictions dict.
        Args:
            batch (dict): dictionary with torch.Tensors sampled
                from a data loader and filtered by @process_batch_for_training
        Returns:
            predictions (dict): dictionary containing network outputs
        """

        qpos, images, env_state, actions, is_pad = self._robomimic_to_act_data(batch, self.camera_keys, self.proprio_keys)

        a_hat, is_pad_hat, (mu, logvar) = self.nets["policy"](qpos, images, env_state, actions=actions, is_pad=is_pad)
        total_kld, dim_wise_kld, mean_kld = self.kl_divergence(mu, logvar)
        loss_dict = dict()
        all_l1 = F.l1_loss(actions, a_hat, reduction='none')
        l1 = (all_l1 * ~is_pad.unsqueeze(-1)).mean()
        loss_dict['l1'] = l1
        loss_dict['kl'] = total_kld[0]


        predictions = OrderedDict(
            actions=actions,
            kl_loss=loss_dict['kl'],
            reconstruction_loss=loss_dict['l1'],
        )

        return predictions
    
    def forward_eval(self, batch):
        """
        Internal helper function for BC algo class. Compute forward pass
        and return network outputs in @predictions dict.
        Args:
            batch (dict): dictionary with torch.Tensors sampled
                from a data loader and filtered by @process_batch_for_training
        Returns:
            predictions (dict): dictionary containing network outputs
        """

        qpos, images, env_state, actions, is_pad = self._robomimic_to_act_data(batch, self.camera_keys, self.proprio_keys)
        a_hat, is_pad_hat, (mu, logvar) = self.nets["policy"](qpos, images, env_state, actions=None, is_pad=is_pad)

        predictions = OrderedDict(
            actions=a_hat
        )

        return predictions

    def get_action(self, obs_dict, goal_dict=None):
        """
        Get policy action outputs.
        Args:
            obs_dict (dict): current observation
            goal_dict (dict): (optional) goal
        Returns:
            action (torch.Tensor): action tensor
        """
        assert not self.nets.training

        proprio = [obs_dict[k] for k in self.proprio_keys]
        proprio = torch.cat(proprio, axis=1)
        qpos = proprio

        images = []
        for cam_name in self.camera_keys:
            image = obs_dict[cam_name]
            image = self.normalize(image)
            image = image.unsqueeze(axis=1)
            images.append(image)
        images = torch.cat(images, axis=1)

        env_state = torch.zeros([qpos.shape[0], 10]).cuda() # not used

        if self._step_counter % self.query_frequency == 0:
            a_hat, is_pad_hat, (mu, logvar) = self.nets["policy"](qpos, images, env_state)
            self.a_hat_store = a_hat

        action = self.a_hat_store[:, self._step_counter % self.query_frequency, :]
        self._step_counter += 1
        return action


    def reset(self):
        """
        Reset algo state to prepare for environment rollouts.
        """
        self._step_counter = 0

    def _compute_losses(self, predictions, batch):
        """
        Internal helper function for BC algo class. Compute losses based on
        network outputs in @predictions dict, using reference labels in @batch.
        Args:
            predictions (dict): dictionary containing network outputs, from @_forward_training
            batch (dict): dictionary with torch.Tensors sampled
                from a data loader and filtered by @process_batch_for_training
        Returns:
            losses (dict): dictionary of losses computed over the batch
        """

        # total loss is sum of reconstruction and KL, weighted by beta
        kl_loss = predictions["kl_loss"]
        recons_loss = predictions["reconstruction_loss"]
        action_loss = recons_loss + self.kl_weight * kl_loss
        return OrderedDict(
            recons_loss=recons_loss,
            kl_loss=kl_loss,
            action_loss=action_loss,
        )

    def log_info(self, info):
        """
        Process info dictionary from @train_on_batch to summarize
        information to pass to tensorboard for logging.
        Args:
            info (dict): dictionary of info
        Returns:
            loss_log (dict): name -> summary statistic
        """
        log = PolicyAlgo.log_info(self, info)
        log["Loss"] = info["losses"]["action_loss"].item()
        log["KL_Loss"] = info["losses"]["kl_loss"].item()
        log["Reconstruction_Loss"] = info["losses"]["recons_loss"].item()
        if "policy_grad_norms" in info:
            log["Policy_Grad_Norms"] = info["policy_grad_norms"]
        return log

    def kl_divergence(self, mu, logvar):
        batch_size = mu.size(0)
        assert batch_size != 0
        if mu.data.ndimension() == 4:
            mu = mu.view(mu.size(0), mu.size(1))
        if logvar.data.ndimension() == 4:
            logvar = logvar.view(logvar.size(0), logvar.size(1))

        klds = -0.5 * (1 + logvar - mu.pow(2) - logvar.exp())
        total_kld = klds.sum(1).mean(0, True)
        dimension_wise_kld = klds.mean(0)
        mean_kld = klds.mean(1).mean(0, True)

        return total_kld, dimension_wise_kld, mean_kld


class ACTSP(ACT):
    def _create_networks(self):
        super(ACTSP, self)._create_networks()
        self.proprio_keys_hand = self.global_config.observation_hand.modalities.obs.low_dim.copy()

        # self.proprio_dim = 0
        # for k in self.proprio_keys_hand:
        #     self.proprio_dim_hand += self.obs_key_shapes[k][0]

    def build_model_opt(self, policy_config):
        return build_single_policy_model_and_optimizer(policy_config)

    def _forward_training(self, batch):
        """
        Internal helper function for BC algo class. Compute forward pass
        and return network outputs in @predictions dict.
        Args:
            batch (dict): dictionary with torch.Tensors sampled
                from a data loader and filtered by @process_batch_for_training
        Returns:
            predictions (dict): dictionary containing network outputs
        """

        modality = self._modality_check(batch)
        if modality == "hand":
            batch["actions"] = batch["actions"][:, :, :3] # HACK bc current ds is 30 dim
        cam_keys = self.camera_keys if modality == "robot" else self.camera_keys[:1] #TODO Simar rm hardcoding
        proprio_keys = self.proprio_keys_hand if modality == "hand" else self.proprio_keys
        qpos, images, env_state, actions, is_pad = self._robomimic_to_act_data(batch, cam_keys, proprio_keys)

        a_hat, is_pad_hat, (mu, logvar) = self.nets["policy"](qpos, images, env_state, modality, actions=actions, is_pad=is_pad)
        total_kld, dim_wise_kld, mean_kld = self.kl_divergence(mu, logvar)
        loss_dict = dict()
        all_l1 = F.l1_loss(actions, a_hat, reduction='none')
        l1 = (all_l1 * ~is_pad.unsqueeze(-1)).mean()
        loss_dict['l1'] = l1
        loss_dict['kl'] = total_kld[0]

        predictions = OrderedDict(
            actions=actions,
            kl_loss=loss_dict['kl'],
            reconstruction_loss=loss_dict['l1'],
        )

        return predictions
    
    def forward_eval(self, batch):
        """
        Internal helper function for BC algo class. Compute forward pass
        and return network outputs in @predictions dict.
        Args:
            batch (dict): dictionary with torch.Tensors sampled
                from a data loader and filtered by @process_batch_for_training
        Returns:
            predictions (dict): dictionary containing network outputs
        """

        modality = self._modality_check(batch)
        #TODO: remove hardcoding, this is because the current dataset I'm using lacks the type label
        if "right_wrist_img" in batch["obs"]:
            modality = "robot"
        else:
            modality = "hand"
        if modality == "hand":
            batch["actions"] = batch["actions"][:, :, :3] # HACK bc current ds is 30 dim

        cam_keys = self.camera_keys if modality == "robot" else self.camera_keys[:1] #TODO Simar rm hardcoding
        proprio_keys = self.proprio_keys_hand if modality == "hand" else self.proprio_keys
        qpos, images, env_state, actions, is_pad = self._robomimic_to_act_data(batch, cam_keys, proprio_keys)
        a_hat, is_pad_hat, (mu, logvar) = self.nets["policy"](qpos, images, env_state, modality, actions=None, is_pad=is_pad)

        predictions = OrderedDict(
            actions=a_hat
        )

        return predictions