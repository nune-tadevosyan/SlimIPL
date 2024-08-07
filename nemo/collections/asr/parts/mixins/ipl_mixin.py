# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import random
import tempfile
from typing import List, Union

import editdistance
import torch
from omegaconf import ListConfig, open_dict
from tqdm.auto import tqdm

from nemo.collections.asr.data.audio_to_text import expand_sharded_filepaths
from nemo.collections.asr.parts.utils.ipl_utils import (
    create_final_cache_manifest,
    expand_braces,
    formulate_cache_manifest_names,
    handle_multiple_tarr_filepaths,
    process_manifest,
    rm_punctuation,
    sample_data,
    write_cache_manifest,
    write_tar_cache_manifest,
)


class IPLMixin:
    """
    Adds ability to do iterative pseudo-labeling.
    To use ipl parameters should be given in the config as well as max_steps should be proovided in trainer.
    For details, see: SlimIPL:(https://arxiv.org/pdf/2010.11524).

    Parameters in config.
    ipl:
        m_epochs (int): Number of epochs to train model before first PL generation.
        restore_pc (bool): Whether tp restore PC by comparing with already existing transcriptions if there are any. Defaults to `False`.
        manifest_filepath (str): Path to the dataset manifest file.
        tarred_audio_filepaths (str): Path to the tarred audio files.
        is_tarred (bool): Flag indicating whether the dataset is tarred.
        dataset_weights (float or list): What part of the dataset to use (applicable with non-tar datasets). Defaults to 1.
        limit_train_batches (int): Limit_train_batches after PLs are added to train set (for lhotse only).
        cache_manifest (str): Path for cache the manifest file.
        dropout (float): Dropout rate used during training after PL generation.
        n_l_epochs (int): Number of epochs to train the model with changed dropout before adding PLs to train set.
        p_cache (float): Probability with which cache will be updated
        cache_prefix (str): Prefix for cache files (optional for non-tar datasets).
        batch_size (int): Batch size with which PLs will be generated

    Call

        * ``self.setup_ipl(_ipl_model_type)``
          in the init method
        * ``self.maybe_do_ipl()``
          in the `on_train_epoch_end` method.
    """

    def setup_ipl(self, model_type: str):
        """
        Sets up IPL configuration for the model.

        Args:
            _ipl_model_type (str): The type of model being used. Takes values "hybrid" or "ctc".
        """

        ipl_config = self.cfg.get("ipl")
        self._ipl_params = {}
        self._ipl_model_type = model_type
        if ipl_config is not None:
            if self.trainer and self.trainer.max_steps < 0:
                raise ValueError(" For IPL to work max steps should be provided in the trainer.")
            self._set_ipl_params(ipl_config)
            if self.cfg.train_ds.get("is_tarred", False):
                self._ipl_params['cache_manifest'] = []
                self._ipl_params['all_cache_manifests'] = formulate_cache_manifest_names(
                    self._ipl_params['manifest_filepath'], self._ipl_params['cache_prefix'], is_tarred=True
                )
            else:
                if not self._ipl_params.get("cache_manifest", None):
                    self._ipl_params['cache_manifest'] = formulate_cache_manifest_names(
                        self._ipl_params['manifest_filepath'], self._ipl_params['cache_prefix'], is_tarred=False
                    )

    def _set_ipl_params(self, ipl_config):
        """
        Processes and sets IPL parameters from the configuration.

        Args:
            ipl_config (DictConfig): The configuration dictionary for IPL parameters.
        """
        required_params = {'m_epochs', 'manifest_filepath', 'is_tarred', 'dropout', 'n_l_epochs', 'p_cache'}

        supported_params = {
            'm_epochs',
            'restore_pc',
            'manifest_filepath',
            'tarred_audio_filepaths',
            'is_tarred',
            'dataset_weights',
            'dropout',
            'limit_train_batches',
            'cache_manifest',
            'n_l_epochs',
            'p_cache',
            'cache_prefix',
            'batch_size',
        }

        for param_name, param_value in ipl_config.items():
            if param_name in supported_params:
                self._ipl_params[param_name] = param_value
            else:
                logging.warning(f"Unsupported IPL parameter: {param_name}. This parameter will be ignored.")

        # Check for missing required parameters
        missing_params = required_params - self._ipl_params.keys()
        if missing_params:
            raise ValueError(f"Missing required IPL parameters: {missing_params}")

        # Log a warning for any extra parameters in the configuration
        extra_params = set(ipl_config.keys()) - supported_params
        if extra_params:
            logging.warning(f"Extra IPL parameters found in configuration and will be ignored: {extra_params}")

    def maybe_do_ipl(self):
        """
        Function implements the logic of IPL algorithm.
        """
        if not self.cfg.get("ipl"):
            return
        if self._ipl_params['m_epochs'] > 0:
            self._ipl_params['m_epochs'] -= 1
            return
        needs_update = True
        if self._ipl_params['m_epochs'] == 0:
            self.build_cache(update_whole_cache=True)

            self.encoder.set_dropout(self._ipl_params['dropout'])
            self._ipl_params['m_epochs'] -= 1
            needs_update = False

        if self._ipl_params['m_epochs'] == -1 and self._ipl_params['n_l_epochs'] > 0:
            self._ipl_params['n_l_epochs'] -= 1
        else:
            if needs_update:
                self.build_cache(update_whole_cache=False)

            if self._ipl_params['n_l_epochs'] == 0:
                self.update_training_sets()
                self._ipl_params['n_l_epochs'] = -1
                self.trainer.reload_dataloaders_every_n_epochs = 1

            if torch.distributed.is_initialized():
                torch.distributed.barrier()
            with open_dict(self.cfg.train_ds):
                self.cfg.train_ds.cache_audio = False
                if self.cfg.train_ds.get("use_lhotse", False):
                    self.cfg.train_ds.update_limit_train_batches = False
                    self.setup_training_data(self.cfg.train_ds)
                else:
                    self.cfg.train_ds.update_limit_train_batches = True
                    self.setup_training_data(self.cfg.train_ds)

    def update_training_sets(self):
        """
        Adds pseudo labeled sets to training datasets based on dataset type.
        """
        if self.cfg.train_ds.get("is_tarred", False):
            final_cache_manifests = self.combine_cache_hypotheses()
        if self.cfg.train_ds.get("is_tarred", False):
            if isinstance(self._ipl_params['tarred_audio_filepaths'], str):
                if isinstance(self.cfg.train_ds['tarred_audio_filepaths'], str):
                    self.cfg.train_ds['tarred_audio_filepaths'] = [
                        [self.cfg.train_ds['tarred_audio_filepaths']],
                        [self._ipl_params['tarred_audio_filepaths']],
                    ]
                else:
                    self.cfg.train_ds.tarred_audio_filepaths.append([self._ipl_params['tarred_audio_filepaths']])
            else:
                if isinstance(self.cfg.train_ds.tarred_audio_filepaths, str):
                    self.cfg.train_ds.tarred_audio_filepaths = ListConfig([[self.cfg.train_ds.tarred_audio_filepaths]])
                self.cfg.train_ds.tarred_audio_filepaths += self._ipl_params['tarred_audio_filepaths']

            if isinstance(self.cfg.train_ds.manifest_filepath, str):
                self.cfg.train_ds.manifest_filepath = ListConfig([[self.cfg.train_ds.manifest_filepath]])

            self.cfg.train_ds.manifest_filepath += final_cache_manifests
            if self._ipl_params.get("limit_train_batches", None):
                self.trainer.limit_train_batches = self._ipl_params["limit_train_batches"]

        else:
            if isinstance(self.cfg.train_ds.manifest_filepath, str):
                self.cfg.train_ds.manifest_filepath = ListConfig([self.cfg.train_ds.manifest_filepath])
                self.cfg.train_ds.manifest_filepath.append(self._ipl_params['cache_manifest'])
            else:
                self.cfg.train_ds.manifest_filepath.append(self._ipl_params['cache_manifest'])

    def build_cache(self, update_whole_cache: bool):
        """
        Function to build cache file for maintaining pseudo labels.
        Args:
            update_whole_cache: (bool) Indicates whether to update the entire cache or only a portion of it based on sampling.
        """
        if self.cfg.train_ds.get("is_tarred", False):

            if update_whole_cache:
                self.create_tar_cache_hypotheses(
                    self._ipl_params['manifest_filepath'], self._ipl_params['tarred_audio_filepaths']
                )
            else:
                self.update_tar_cache_hypotheses(
                    self._ipl_params['all_cache_manifests'], self._ipl_params['tarred_audio_filepaths']
                )
        else:
            self.create_cache_hypotheses(self._ipl_params['manifest_filepath'], update_whole_cache)

    def create_cache_hypotheses(self, manifests: Union[List[List[str]], str], update_whole_cache: bool = True):
        """
        Function to create cache file for unlabeled dataset
        Args:
            update_whole_cache: Indicates whether to update the entire cache or only a portion of it based on sampling.
            manifests:  Manifest file(s) from which pseudo labels will be generated
        """
        whole_pseudo_data = []
        update_data = []

        manifest_paths = [manifests] if isinstance(manifests, str) else manifests
        dataset_weights = self._ipl_params.get("dataset_weights", [1] * len(manifest_paths))

        if not isinstance(dataset_weights, ListConfig) and not isinstance(dataset_weights, List):
            dataset_weights = [float(dataset_weights)]

        for idx, manifest_path in enumerate(manifest_paths):
            manifest_data = process_manifest(manifest_path)
            whole_pseudo_data.extend(manifest_data)
            weight = dataset_weights[idx] if idx < len(dataset_weights) else 1
            update_data.extend(sample_data(manifest_data, weight, update_whole_cache, self._ipl_params['p_cache']))

        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_manifest = os.path.join(
                tmpdir, f'manifest_{torch.distributed.get_rank() if torch.distributed.is_initialized() else 0}.json'
            )
            with open(temporary_manifest, 'w', encoding='utf-8') as temp_manifest:
                transcriptions = [data_entry.get('text', "") for data_entry in update_data]
                for data_entry in update_data:
                    json.dump(data_entry, temp_manifest, ensure_ascii=False)
                    temp_manifest.write('\n')
            if self._ipl_model_type == "hybrid":
                hypotheses = self.generate_pseudo_labels_hybrid(
                    temporary_manifest,
                    target_transcripts=transcriptions,
                    restore_pc=self._ipl_params['restore_pc'],
                    batch_size=self._ipl_params['batch_size'],
                )
            else:
                hypotheses = self.generate_pseudo_labels_ctc(
                    temporary_manifest,
                    target_transcripts=transcriptions,
                    restore_pc=self._ipl_params['restore_pc'],
                    batch_size=self._ipl_params['batch_size'],
                )

        if torch.distributed.is_initialized():
            torch.distributed.barrier()
            gathered_hypotheses = [None] * torch.distributed.get_world_size()
            gathered_data = [None] * torch.distributed.get_world_size()
            torch.distributed.all_gather_object(gathered_data, update_data)
            torch.distributed.all_gather_object(gathered_hypotheses, hypotheses)
            if torch.distributed.get_rank() == 0:
                write_cache_manifest(
                    self._ipl_params['cache_manifest'], gathered_hypotheses, gathered_data, update_whole_cache
                )
            torch.distributed.barrier()
        else:
            write_cache_manifest(self._ipl_params['cache_manifest'], [hypotheses], [update_data], update_whole_cache)

    def create_tar_cache_hypotheses(
        self, manifests: Union[List[List[str]], str], tarred_audio_filepaths: Union[List[List[str]], str]
    ):
        """
        Function to create cache file for tarred unlabeled dataset for the first time
        Args:
            manifests:  Manifest file(s) from which pseudo labels will be generated
            tarred_audio_filepaths: Tar file paths for tarred datasets
        """
        if isinstance(manifests, str):
            manifests = [[manifests]]

        if isinstance(tarred_audio_filepaths, str):
            tarred_audio_filepaths = [[tarred_audio_filepaths]]

        self._ipl_params['cache_manifest'] = []
        for manifest, tarred_audio_filepath in zip(manifests, tarred_audio_filepaths):
            with tempfile.TemporaryDirectory() as tmpdir:

                if torch.distributed.is_initialized():
                    torch.distributed.barrier()

                expanded_audio = expand_sharded_filepaths(
                    tarred_audio_filepath[0],
                    shard_strategy='scatter',
                    world_size=self.world_size,
                    global_rank=self.global_rank,
                )
                expand_manifests = expand_sharded_filepaths(
                    manifest[0], shard_strategy='scatter', world_size=self.world_size, global_rank=self.global_rank
                )
                number_of_manifests = len(expand_manifests)

                shard_manifest_data = []
                cache_manifest = []
                transcriptions = []
                for _, manifest_path in enumerate(expand_manifests):

                    manifest_data = process_manifest(manifest_path)
                    shard_manifest_data.append(manifest_data)

                    base_path, filename = os.path.split(manifest_path)
                    cache_file = os.path.join(base_path, f'{self._ipl_params["cache_prefix"]}_cache_{filename}')
                    cache_manifest.append(cache_file)

                    temporary_manifest = os.path.join(tmpdir, f'temp_{filename}')

                    with open(temporary_manifest, 'w', encoding='utf-8') as temp_manifest:

                        for data_entry in manifest_data:
                            if not data_entry.get("text", None):
                                data_entry['text'] = ""
                            transcriptions.append(data_entry.get('text', ""))
                            json.dump(data_entry, temp_manifest, ensure_ascii=False)
                            temp_manifest.write('\n')

                if number_of_manifests > 1:
                    temporary_manifest, expanded_audio = handle_multiple_tarr_filepaths(
                        filename, tmpdir, number_of_manifests, expanded_audio[0]
                    )
                else:
                    expanded_audio = expanded_audio[0]
                if self._ipl_model_type == "hybrid":
                    hypotheses = self.generate_pseudo_labels_hybrid(
                        cache_manifest=temporary_manifest,
                        tarred_audio_filepaths=expanded_audio,
                        target_transcripts=None,
                        restore_pc=self._ipl_params['restore_pc'],
                        batch_size=self._ipl_params['batch_size'],
                    )
                else:
                    hypotheses = self.generate_pseudo_labels_ctc(
                        cache_manifest=temporary_manifest,
                        tarred_audio_filepaths=expanded_audio,
                        target_transcripts=transcriptions,
                        restore_pc=self._ipl_params['restore_pc'],
                        batch_size=self._ipl_params['batch_size'],
                    )
                write_tar_cache_manifest(
                    cache_manifest,
                    update_data=shard_manifest_data,
                    hypotheses=hypotheses,
                    use_lhotse=self.cfg.train_ds.get('use_lhotse', False),
                )
                if torch.distributed.is_initialized():
                    torch.distributed.barrier()
                self._ipl_params['cache_manifest'].append(cache_manifest)

    def update_tar_cache_hypotheses(
        self, manifests: Union[List[List[str]], str], tarred_audio_filepaths: Union[List[List[str]], str]
    ):
        """
        With given probability randomly chooses part of the cache hypotheses, generates new pseudo labels for them and updates the cache.
        Args:
            manifests: Cache manifest files where pseudo labels are kept.
            tarred_audio_filepaths: Path to tarred audio files.
        """
        if isinstance(manifests, str):
            manifests = [[manifests]]

        if isinstance(tarred_audio_filepaths, str):
            tarred_audio_filepaths = [[tarred_audio_filepaths]]
        if torch.distributed.is_initialized():
            torch.distributed.barrier()
        for manifest, tarred_audio_filepath in zip(manifests, tarred_audio_filepaths):
            with tempfile.TemporaryDirectory() as tmpdir:
                expanded_audio = expand_sharded_filepaths(
                    tarred_audio_filepath[0],
                    shard_strategy='scatter',
                    world_size=self.world_size,
                    global_rank=self.global_rank,
                )
                manifest = expand_sharded_filepaths(
                    manifest[0], shard_strategy='scatter', world_size=self.world_size, global_rank=self.global_rank
                )
                shard_manifest_data = []
                number_of_manifests = len(manifest)
                all_indices = []
                for _, manifest_path in enumerate(manifest):

                    manifest_data = process_manifest(manifest_path)
                    update_size = int(len(manifest_data) * self._ipl_params['p_cache'])
                    random.seed()
                    indices = random.sample(range(len(manifest_data)), update_size)
                    update_data = [manifest_data[index] for index in indices]
                    shard_manifest_data.append(manifest_data)

                    all_indices.append(indices)
                    _, filename = os.path.split(manifest_path)
                    temporary_manifest = os.path.join(tmpdir, f'temp_{filename}')

                    with open(temporary_manifest, 'w', encoding='utf-8') as temp_manifest:
                        transcriptions = []
                        for data_entry in update_data:
                            transcriptions.append(data_entry.get('text', ""))
                            json.dump(data_entry, temp_manifest, ensure_ascii=False)
                            temp_manifest.write('\n')
                if number_of_manifests > 1:
                    temporary_manifest, expanded_audio = handle_multiple_tarr_filepaths(
                        filename, tmpdir, number_of_manifests, expanded_audio[0]
                    )
                else:
                    expanded_audio = expanded_audio[0]
                if self._ipl_model_type == "hybrid":
                    hypotheses = self.generate_pseudo_labels_hybrid(
                        temporary_manifest,
                        tarred_audio_filepaths=expanded_audio,
                        target_transcripts=transcriptions,
                        restore_pc=self._ipl_params['restore_pc'],
                        batch_size=self._ipl_params['batch_size'],
                    )
                else:
                    hypotheses = self.generate_pseudo_labels_ctc(
                        temporary_manifest,
                        tarred_audio_filepaths=expanded_audio,
                        target_transcripts=transcriptions,
                        restore_pc=self._ipl_params['restore_pc'],
                        batch_size=self._ipl_params['batch_size'],
                    )
            write_tar_cache_manifest(
                manifest,
                update_data=shard_manifest_data,
                hypotheses=hypotheses,
                indices=all_indices,
                update_size=update_size,
                use_lhotse=self.cfg.train_ds.get('use_lhotse', False),
            )
            if torch.distributed.is_initialized():
                torch.distributed.barrier()

    def combine_cache_hypotheses(self):
        """
        For each dataset combines cache hypotheses from manifests into one final cache manifest.
        Returns:
            final_cache_manifests: List of final cache manifests.
        """
        final_cache_manifests = []
        if self.cfg.train_ds.get("is_tarred", False):

            if not self.cfg.train_ds.get("use_lhotse", False):
                for manifests in self._ipl_params['all_cache_manifests']:
                    base_path, _ = os.path.split(manifests[0])
                    final_cache = os.path.join(
                        base_path, f'{self._ipl_params["cache_prefix"]}_cache_tarred_audio_manifest.json'
                    )
                    if torch.distributed.is_initialized():
                        if torch.distributed.get_rank() == 0:
                            create_final_cache_manifest(final_cache, manifests[0])
                        torch.distributed.barrier()
                    else:
                        create_final_cache_manifest(final_cache, manifests[0])
                    final_cache_manifests.append([final_cache])
            else:
                for i_dataset in self._ipl_params['all_cache_manifests']:
                    i_dataset = expand_braces(i_dataset)
                    num_manifests = len(i_dataset)
                    base_path, file_name = os.path.split(i_dataset[0])
                    base_file_name = file_name.rsplit('_', 1)[0]
                    dataset_manifests = os.path.join(base_path, f"{base_file_name}_{{{0}..{num_manifests-1}}}.json")
                    final_cache_manifests.append([dataset_manifests])

        return final_cache_manifests

    def generate_pseudo_labels_hybrid(
        self,
        cache_manifest: Union[List[List[str]], str],
        tarred_audio_filepaths: Union[List[List[str]], str] = None,
        restore_pc: bool = True,
        target_transcripts: List[str] = None,
        batch_size: int = 64,
    ):
        """
        Generates pseudo labels for unlabeled data for Hybrid models.
        Args:
            cache_manifest: Temprorary cache file with sampled data.
            tarred_audio_filepaths: Path to tar audio files.
            restore_pc: Whether to restore PC for transcriptions that do not have any.
            target_transcripts: Already existing transcriptions that can be used for restoring PC.c
            batch_size: Batch size used for during inference.
        Returns:
            hypotheses: List of generated labels.
        """
        device = next(self.parameters()).device
        dither_value = self.preprocessor.featurizer.dither
        pad_to_value = self.preprocessor.featurizer.pad_to

        self.eval()
        self.encoder.freeze()
        self.decoder.freeze()
        self.joint.freeze()
        self.ctc_decoder.freeze()
        hypotheses = []

        dataloader = self._setup_pseudo_label_dataloader(cache_manifest, tarred_audio_filepaths, batch_size)
        self.preprocessor.featurizer.dither = 0.0
        self.preprocessor.featurizer.pad_to = 0
        sample_idx = 0
        count = 0
        for test_batch in tqdm(dataloader, desc="Transcribing"):
            count += 1
            encoded, encoded_len = self.forward(
                input_signal=test_batch[0].to(device), input_signal_length=test_batch[1].to(device)
            )

            logits = self.ctc_decoder(encoder_output=encoded)
            logits = logits.cpu()
            if self.cfg.aux_ctc.decoding.strategy == "beam":

                best_hyp, all_hyp = self.ctc_decoding.ctc_decoder_predictions_tensor(
                    logits,
                    encoded_len,
                    return_hypotheses=True,
                )
                if all_hyp:
                    for beams_idx, beams in enumerate(all_hyp):
                        if restore_pc:
                            target = target_transcripts[sample_idx + beams_idx]
                            if target != "":
                                target_split_w = target.split()
                                wer_dist_min = 1000
                                min_pred_text = ""
                                for _, candidate in enumerate(beams):
                                    pred_text = candidate.text
                                    compare_text = pred_text
                                    compare_text = compare_text.lower()
                                    compare_text = rm_punctuation(compare_text, ",.?")
                                    pred_split_w = compare_text.split()
                                    wer_dist = editdistance.eval(target_split_w, pred_split_w)
                                    if wer_dist < wer_dist_min:
                                        min_pred_text = pred_text
                                        wer_dist_min = wer_dist
                                hypotheses.append(min_pred_text)
                            else:
                                hypotheses.append(best_hyp[beams_idx].text)
                        else:
                            hypotheses.append(best_hyp[beams_idx].text)
                    sample_idx += logits.shape[0]
                else:
                    hypotheses += [hyp.text for hyp in best_hyp]
            else:
                best_hyp, all_hyp = self.ctc_decoding.ctc_decoder_predictions_tensor(
                    logits,
                    encoded_len,
                    return_hypotheses=False,
                )
                hypotheses += best_hyp

            del logits
            del encoded
            del test_batch

        self.train()
        self.preprocessor.featurizer.dither = dither_value
        self.preprocessor.featurizer.pad_to = pad_to_value

        self.encoder.unfreeze()
        self.decoder.unfreeze()
        self.joint.unfreeze()

        self.ctc_decoder.unfreeze()
        return hypotheses

    def generate_pseudo_labels_ctc(
        self,
        cache_manifest: Union[List[List[str]], str],
        tarred_audio_filepaths: Union[List[List[str]], str] = None,
        restore_pc: bool = True,
        target_transcripts: List[str] = None,
        batch_size: int = 64,
    ):
        """
        Generates pseudo labels for unlabeled data for CTC only models.
        Args:
            cache_manifest: Temprorary cache file with sampled data.
            tarred_audio_filepaths: Path to tar audio files.
            restore_pc: Whether to restore PC for transcriptions that do not have any.
            target_transcripts: Already existing transcriptions that can be used for restoring PC.c
            batch_size: Batch size used for during inference.
        Returns:
            hypotheses: List of generated labels.
        """
        device = next(self.parameters()).device
        dither_value = self.preprocessor.featurizer.dither
        pad_to_value = self.preprocessor.featurizer.pad_to

        self.eval()
        self.encoder.freeze()
        self.decoder.freeze()
        hypotheses = []

        dataloader = self._setup_pseudo_label_dataloader(cache_manifest, tarred_audio_filepaths, batch_size)

        self.preprocessor.featurizer.dither = 0.0
        self.preprocessor.featurizer.pad_to = 0
        sample_idx = 0

        for test_batch in tqdm(dataloader, desc="Transcribing"):
            logits, logits_len, _ = self.forward(
                input_signal=test_batch[0].to(device), input_signal_length=test_batch[1].to(device)
            )

            logits = logits.cpu()
            if self.cfg.decoding.strategy == "beam":
                best_hyp, all_hyp = self.decoding.ctc_decoder_predictions_tensor(
                    logits,
                    logits_len,
                    return_hypotheses=True,
                )
                if all_hyp:
                    for beams_idx, beams in enumerate(all_hyp):
                        target = target_transcripts[sample_idx + beams_idx]
                        if target and restore_pc:
                            target_split_w = target.split()
                            wer_dist_min = 1000
                            min_pred_text = ""
                            for _, candidate in enumerate(beams):
                                pred_text = candidate.text
                                compare_text = pred_text
                                compare_text = compare_text.lower()
                                compare_text = rm_punctuation(compare_text, ",.?")
                                pred_split_w = compare_text.split()
                                wer_dist = editdistance.eval(target_split_w, pred_split_w)
                                if wer_dist < wer_dist_min:
                                    min_pred_text = pred_text
                                    wer_dist_min = wer_dist
                            hypotheses.append(min_pred_text)
                        else:

                            hypotheses.append(best_hyp[beams_idx].text)
                    sample_idx += logits.shape[0]
                else:
                    hypotheses += [hyp.text for hyp in best_hyp]
            else:
                best_hyp, all_hyp = self.decoding.ctc_decoder_predictions_tensor(
                    logits,
                    logits_len,
                    return_hypotheses=False,
                )
                hypotheses += best_hyp
            del logits
            del logits_len
            del test_batch

        self.train()
        self.preprocessor.featurizer.dither = dither_value
        self.preprocessor.featurizer.pad_to = pad_to_value

        self.encoder.unfreeze()
        self.decoder.unfreeze()
        return hypotheses
