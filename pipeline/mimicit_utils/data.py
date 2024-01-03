import ast
import functools
import io
import json
import logging
import math
import os
import random
import statistics
import sys
from dataclasses import dataclass
from multiprocessing import Value

import braceexpand
import numpy as np
import torch
import torch.utils
import torchvision
import webdataset as wds
import yaml
from PIL import Image, ImageFile, ImageSequence
from torch.utils.data import ConcatDataset, DataLoader, IterableDataset, RandomSampler, get_worker_info
from torch.utils.data.distributed import DistributedSampler
from webdataset.filters import _shuffle
from webdataset.tariterators import base_plus_ext, tar_file_expander, url_opener, valid_sample

sys.path.append("../..")
import json
import os

import yaml
from PIL import Image, ImageFile

from pipeline.mimicit_utils.llava_pretrain_dataset import LlavaPretrainDataset
from pipeline.mimicit_utils.mimicit_dataset import MimicitDataset
from pipeline.train.train_utils import DistributedProxySampler

Image.MAX_IMAGE_PIXELS = 1000000000
MAX_NUM_TOKENS = 256
MAX_NUM_IMAGES = 5
TINY_IMAGE_SIZE_THRESHOLD = 1
N_CHANNELS = 3
INTERLEAVED_IMAGE_SIZE = 224


class SharedEpoch:
    def __init__(self, epoch: int = 0):
        self.shared_epoch = Value("i", epoch)

    def set_value(self, epoch):
        self.shared_epoch.value = epoch

    def get_value(self):
        return self.shared_epoch.value


@dataclass
class DataInfo:
    dataloader: DataLoader
    sampler: DistributedSampler = None
    shared_epoch: SharedEpoch = None

    def set_epoch(self, epoch):
        if self.shared_epoch is not None:
            self.shared_epoch.set_value(epoch)
        if self.sampler is not None and isinstance(self.sampler, DistributedSampler):
            self.sampler.set_epoch(epoch)


def get_dataset_size(shards):
    shards_list = list(braceexpand.braceexpand(shards))
    dir_path = os.path.dirname(shards_list[0])
    sizes_filename = os.path.join(dir_path, "sizes.json")
    len_filename = os.path.join(dir_path, "__len__")
    if os.path.exists(sizes_filename):
        sizes = json.load(open(sizes_filename, "r"))
        total_size = sum([int(sizes[os.path.basename(shard)]) if os.path.basename(shard) in sizes else 0 for shard in shards_list])
    elif os.path.exists(len_filename):
        # FIXME this used to be eval(open(...)) but that seemed rather unsafe
        total_size = ast.literal_eval(open(len_filename, "r").read())
    else:
        total_size = None  # num samples undefined
        # some common dataset sizes (at time of authors last download)
        # CC3M (train): 2905954
        # CC12M: 10968539
        # LAION-400M: 407332084
        # LAION-2B (english): 2170337258
    num_shards = len(shards_list)
    return total_size, num_shards


def count_samples(dataloader):
    os.environ["WDS_EPOCH"] = "0"
    n_elements, n_batches = 0, 0
    for images, texts in dataloader:
        n_batches += 1
        n_elements += len(images)
        assert len(images) == len(texts)
    return n_elements, n_batches


def filter_no_caption_or_no_image(sample):
    return ("txt" in sample) and ("png" in sample or "jpg" in sample or "jpeg" in sample)


def decode_base64_image(key, value):
    if not key.endswith(".png"):
        return None
    rawbytes = base64.b64decode(value)
    image = Image.open(io.BytesIO(rawbytes))
    # Check if the image is in palette mode and has transparency
    if image.mode == "P":
        try:
            alpha = image.getchannel("A")
            if alpha.mode == "L":
                image = image.convert("RGBA")
        except ValueError:
            pass
    image = image.convert("RGB")
    return image


def log_and_continue(exn):
    """Call in an exception handler to ignore any exception, issue a warning, and continue."""
    if "No images in sample" in str(exn) or "Only one image in sample" in str(exn):  # Avoid spamming logs with these
        return True
    logging.warning(f"Handling webdataset error ({repr(exn)}). Ignoring.")
    return True


def group_by_keys_nothrow(data, keys=base_plus_ext, lcase=True, suffixes=None, handler=None):
    """Return function over iterator that groups key, value pairs into samples.

    :param keys: function that splits the key into key and extension (base_plus_ext)
    :param lcase: convert suffixes to lower case (Default value = True)
    """
    current_sample = None
    for filesample in data:
        assert isinstance(filesample, dict)
        fname, value = filesample["fname"], filesample["data"]
        prefix, suffix = keys(fname)
        if prefix is None:
            continue
        if lcase:
            suffix = suffix.lower()
        # FIXME webdataset version throws if suffix in current_sample, but we have a potential for
        #  this happening in the current LAION400m dataset if a tar ends with same prefix as the next
        #  begins, rare, but can happen since prefix aren't unique across tar files in that dataset
        if current_sample is None or prefix != current_sample["__key__"] or suffix in current_sample:
            if valid_sample(current_sample):
                yield current_sample
            current_sample = dict(__key__=prefix, __url__=filesample["__url__"])
        if suffixes is None or suffix in suffixes:
            current_sample[suffix] = value
    if valid_sample(current_sample):
        yield current_sample


def tarfile_to_samples_nothrow(src, handler=log_and_continue):
    # NOTE this is a re-impl of the webdataset impl with group_by_keys that doesn't throw
    streams = url_opener(src, handler=handler)
    files = tar_file_expander(streams, handler=handler)
    samples = group_by_keys_nothrow(files, handler=handler)
    return samples


def pytorch_worker_seed(increment=0):
    """get dataloader worker seed from pytorch"""
    worker_info = get_worker_info()
    if worker_info is not None:
        # favour using the seed already created for pytorch dataloader workers if it exists
        seed = worker_info.seed
        if increment:
            # space out seed increments so they can't overlap across workers in different iterations
            seed += increment * max(1, worker_info.num_workers)
        return seed
    # fallback to wds rank based seed
    return wds.utils.pytorch_worker_seed()


_SHARD_SHUFFLE_SIZE = 2000
_SHARD_SHUFFLE_INITIAL = 500
_SAMPLE_SHUFFLE_SIZE = 5000
_SAMPLE_SHUFFLE_INITIAL = 1000


class detshuffle2(wds.PipelineStage):
    def __init__(
        self,
        bufsize=1000,
        initial=100,
        seed=0,
        epoch=-1,
    ):
        self.bufsize = bufsize
        self.initial = initial
        self.seed = seed
        self.epoch = epoch

    def run(self, src):
        if isinstance(self.epoch, SharedEpoch):
            epoch = self.epoch.get_value()
        else:
            # NOTE: this is epoch tracking is problematic in a multiprocess (dataloader workers or train)
            # situation as different workers may wrap at different times (or not at all).
            self.epoch += 1
            epoch = self.epoch
        rng = random.Random()
        if self.seed < 0:
            # If seed is negative, we use the worker's seed, this will be different across all nodes/workers
            seed = pytorch_worker_seed(epoch)
        else:
            # This seed to be deterministic AND the same across all nodes/workers in each epoch
            seed = self.seed + epoch
        rng.seed(seed)
        return _shuffle(src, self.bufsize, self.initial, rng)


class ResampledShards2(IterableDataset):
    """An iterable dataset yielding a list of urls."""

    def __init__(
        self,
        urls,
        nshards=sys.maxsize,
        worker_seed=None,
        deterministic=False,
        epoch=-1,
    ):
        """Sample shards from the shard list with replacement.
        :param urls: a list of URLs as a Python list or brace notation string
        """
        super().__init__()
        urls = wds.shardlists.expand_urls(urls)
        self.urls = urls
        assert isinstance(self.urls[0], str)
        self.nshards = nshards
        self.rng = random.Random()
        self.worker_seed = worker_seed
        self.deterministic = deterministic
        self.epoch = epoch

    def __iter__(self):
        """Return an iterator over the shards."""
        if isinstance(self.epoch, SharedEpoch):
            epoch = self.epoch.get_value()
        else:
            # NOTE: this is epoch tracking is problematic in a multiprocess (dataloader workers or train)
            # situation as different workers may wrap at different times (or not at all).
            self.epoch += 1
            epoch = self.epoch

        if self.deterministic:
            # reset seed w/ epoch if deterministic
            if self.worker_seed is None:
                # pytorch worker seed should be deterministic due to being init by arg.seed + rank + worker id
                seed = pytorch_worker_seed(epoch)
            else:
                seed = self.worker_seed() + epoch
            self.rng.seed(seed)
        for _ in range(self.nshards):
            yield dict(url=self.rng.choice(self.urls))


# import uuid
def preprocess_image(sample, image_processor):
    # uuid_str = str(uuid.uuid4())
    # sample[0].save(f'./archived/images/{uuid_str}.png')
    image = [image_processor.preprocess(s, return_tensors="pt")["pixel_values"] for s in sample]
    image = torch.cat(image, dim=0)
    # apply random horizontal flip wo/w color jitter
    image = torchvision.transforms.RandomHorizontalFlip(p=0.5)(image)
    # image = torchvision.transforms.ColorJitter(brightness=0.5, hue=0.3)(image)
    return image


B_INST, E_INST = "[INST]", "[/INST]"


def preprocess_text(sample, tokenizer, prompt_format="simple"):
    tokenizer.padding_side = "right"
    if prompt_format == "simple":
        sample = [(f"<image>{s.strip()}<|endofchunk|>{tokenizer.eos_token}") for s in sample]
    elif prompt_format == "llama2_inst":
        sample = [(f"<image>{B_INST}please describe this image.{E_INST}{s.strip()}<|endofchunk|>{tokenizer.eos_token}") for s in sample]
    text = tokenizer(
        sample,
        max_length=32,
        padding="longest",
        truncation="only_first",
        return_tensors="pt",
    )
    return text["input_ids"], text["attention_mask"]


MIN_KB = 10
MAX_NUM_IMAGES = 5
import base64


def preprocess_interleaved(sample, tokenizer, clip_processor, sim_threshold, distributed_type="no"):
    info = json.loads(sample[0])
    sentences = info["text_list"]

    images, sentence_ixs = [], []

    for sample_image in info["image_info"]:
        image_base64 = sample_image["image_base64"]
        rawbytes = base64.b64decode(image_base64)

        # filter to images >= 10KB
        if len(rawbytes) // 1000 <= MIN_KB:
            continue
        if sample_image["matched_sim"] < sim_threshold:
            continue
        image = Image.open(io.BytesIO(rawbytes))

        # Check if the image is in palette mode and has transparency
        if image.mode == "P" and "transparency" in image.info:
            try:
                image = image.convert("RGBA")
            except ValueError:
                pass

        image = image.convert("RGB")

        images.append(image)
        sentence_ixs.append(sample_image["matched_text_index"])

    if len(images) == 0:
        raise ValueError("No images in sample")

    # images -> tensors
    images_tensors = preprocess_image(images, clip_processor)
    keep_ixs = range(min(len(images_tensors), MAX_NUM_IMAGES))
    images_tensors = images_tensors[keep_ixs]
    sentence_ixs = [sentence_ixs[ix] for ix in keep_ixs]

    # pad to 5 images
    if len(images_tensors) < MAX_NUM_IMAGES:
        zero_padding = torch.zeros((MAX_NUM_IMAGES - len(images_tensors), 3, 224, 224), dtype=torch.float)
        images_tensors = torch.cat((images_tensors, zero_padding), dim=0)

    # add in <image> and <eoc> tokens
    # eoc after sentence = "sentence loss"
    for ix in sentence_ixs:
        sentences[ix] = f"<|endofchunk|><image>{sentences[ix]}"

    text = " ".join(sentences)
    text = text.replace("<|endofchunk|>", "", 1)  # but remove first eoc
    # whitespace cleanup
    text = text.replace(" <|endofchunk|>", "<|endofchunk|>").replace("<image> ", "<image>").replace(" <image>", "<image>")
    text = f"{text}<|endofchunk|>{tokenizer.eos_token}"
    tokenizer.padding_side = "right"
    text_tensor = tokenizer(text, max_length=256, truncation=True, padding="max_length", return_tensors="pt")

    # reject sequences with too few images (after truncation)
    num_images = torch.count_nonzero(text_tensor["input_ids"] == tokenizer.additional_special_tokens_ids[tokenizer.additional_special_tokens.index("<image>")])

    if num_images == 0:
        raise ValueError("No images in sample")
    elif num_images == 1 and random.random() <= 0.5:  # 50% chance of keeping single image samples
        raise ValueError("Only one image in sample")

    return (
        images_tensors,
        (text_tensor["input_ids"], text_tensor["attention_mask"]),
    )


def get_mmc4_dataset(args, image_processor, tokenizer, epoch=0, floor=False):
    input_shards = args.mmc4_shards
    assert input_shards is not None
    resampled = getattr(args, "dataset_resampled", False)

    num_samples, num_shards = get_dataset_size(input_shards)
    num_samples = None
    if not num_samples:
        num_samples = args.train_num_samples_mmc4
        if not num_samples:
            raise RuntimeError("Currently, number of dataset samples must be specified for training dataset. " "Please specify via `--train-num-samples` if no dataset length info present.")

    # create a shared epoch store to sync epoch to dataloader worker proc
    shared_epoch = SharedEpoch(epoch=epoch)
    if resampled:
        pipeline = [ResampledShards2(input_shards, deterministic=True, epoch=shared_epoch)]
    else:
        pipeline = [wds.SimpleShardList(input_shards)]

    preprocess_fn = functools.partial(
        preprocess_interleaved,
        clip_processor=image_processor,
        tokenizer=tokenizer,
        sim_threshold=args.mmc4_textsim_threshold,
    )

    # at this point we have an iterator over all the shards
    if not resampled:
        pipeline.extend(
            [
                detshuffle2(
                    bufsize=_SHARD_SHUFFLE_SIZE,
                    initial=_SHARD_SHUFFLE_INITIAL,
                    seed=args.seed,
                    epoch=shared_epoch,
                ),
                wds.split_by_node,
                wds.split_by_worker,
            ]
        )
    pipeline.extend(
        [
            # at this point, we have an iterator over the shards assigned to each worker at each node
            # wds.tarfile_to_samples(handler=log_and_continue),
            tarfile_to_samples_nothrow,
            wds.shuffle(
                bufsize=_SAMPLE_SHUFFLE_SIZE,
                initial=_SAMPLE_SHUFFLE_INITIAL,
            ),
        ]
    )

    pipeline.extend(
        [
            wds.to_tuple("json", handler=log_and_continue),
            wds.map(preprocess_fn, handler=log_and_continue),
            wds.batched(args.batch_size_mmc4, partial=False),
        ]
    )

    dataset = wds.DataPipeline(*pipeline)
    if not resampled:
        assert num_shards >= args.workers * args.world_size, "number of shards must be >= total workers"
    # roll over and repeat a few samples to get same number of full batches on each node
    round_fn = math.floor if floor else math.ceil
    global_batch_size = args.batch_size_mmc4 * args.world_size
    num_batches = round_fn(num_samples / global_batch_size)
    num_workers = max(1, args.workers)
    num_worker_batches = round_fn(num_batches / num_workers)  # per dataloader worker
    num_batches = num_worker_batches * num_workers
    num_samples = num_batches * global_batch_size
    # each worker is iterating over this
    dataset = dataset.with_epoch(num_worker_batches)

    dataloader = wds.WebLoader(
        dataset,
        batch_size=None,
        shuffle=False,
        num_workers=args.workers,
        persistent_workers=True,
    )

    # add meta-data to dataloader instance for convenience
    dataloader.num_batches = num_batches
    dataloader.num_samples = num_samples

    return DataInfo(dataloader=dataloader, shared_epoch=shared_epoch)


def get_laion_dataset(args, image_processor, tokenizer, epoch=0, floor=False):
    input_shards = args.laion_shards
    assert input_shards is not None
    resampled = getattr(args, "dataset_resampled", False)

    num_samples, num_shards = get_dataset_size(input_shards)
    num_samples = None
    if not num_samples:
        num_samples = args.train_num_samples_laion
        if not num_samples:
            raise RuntimeError("Currently, number of dataset samples must be specified for training dataset. " "Please specify via `--train-num-samples` if no dataset length info present.")

    # create a shared epoch store to sync epoch to dataloader worker proc
    shared_epoch = SharedEpoch(epoch=epoch)
    if resampled:
        pipeline = [ResampledShards2(input_shards, deterministic=True, epoch=shared_epoch)]
    else:
        pipeline = [wds.SimpleShardList(input_shards)]

    # create two preprocess functions that take in the passed in image_processor and tokenizer
    preprocess_image_fn = functools.partial(preprocess_image, image_processor=image_processor)
    preprocess_text_fn = functools.partial(preprocess_text, tokenizer=tokenizer)

    # at this point we have an iterator over all the shards
    if not resampled:
        pipeline.extend(
            [
                detshuffle2(
                    bufsize=_SHARD_SHUFFLE_SIZE,
                    initial=_SHARD_SHUFFLE_INITIAL,
                    seed=args.seed,
                    epoch=shared_epoch,
                ),
                wds.split_by_node,
                wds.split_by_worker,
            ]
        )
    pipeline.extend(
        [
            # at this point, we have an iterator over the shards assigned to each worker at each node
            # wds.tarfile_to_samples(handler=log_and_continue),
            tarfile_to_samples_nothrow,
            wds.shuffle(
                bufsize=_SAMPLE_SHUFFLE_SIZE,
                initial=_SAMPLE_SHUFFLE_INITIAL,
            ),
        ]
    )

    pipeline.extend(
        [
            wds.select(filter_no_caption_or_no_image),
            wds.decode(decode_base64_image, only="png", handler=log_and_continue),
            wds.to_tuple("jpg;png;jpeg", "txt", handler=log_and_continue),
            wds.batched(args.batch_size_laion, partial=False),
            wds.map_tuple(preprocess_image_fn, preprocess_text_fn, handler=log_and_continue),
        ]
    )

    dataset = wds.DataPipeline(*pipeline)
    if not resampled:
        assert num_shards >= args.workers * args.world_size, "number of shards must be >= total workers"
    # roll over and repeat a few samples to get same number of full batches on each node
    round_fn = math.floor if floor else math.ceil
    global_batch_size = args.batch_size_laion * args.world_size
    num_batches = round_fn(num_samples / global_batch_size)
    num_workers = max(1, args.workers)
    num_worker_batches = round_fn(num_batches / num_workers)  # per dataloader worker
    num_batches = num_worker_batches * num_workers
    num_samples = num_batches * global_batch_size
    # each worker is iterating over this
    dataset = dataset.with_epoch(num_worker_batches)

    dataloader = wds.WebLoader(
        dataset,
        batch_size=None,
        shuffle=False,
        num_workers=args.workers,
        persistent_workers=True,
    )

    # add meta-data to dataloader instance for convenience
    dataloader.num_batches = num_batches
    dataloader.num_samples = num_samples

    return DataInfo(dataloader=dataloader, shared_epoch=shared_epoch)


def get_cc3m_dataset(args, image_processor, tokenizer, epoch=0, floor=False):
    input_shards = args.cc3m_shards
    assert input_shards is not None
    resampled = getattr(args, "dataset_resampled", False)

    num_samples, num_shards = get_dataset_size(input_shards)
    num_samples = None
    if not num_samples:
        num_samples = args.train_num_samples_cc3m
        if not num_samples:
            raise RuntimeError("Currently, number of dataset samples must be specified for training dataset. " "Please specify via `--train-num-samples` if no dataset length info present.")

    # create a shared epoch store to sync epoch to dataloader worker proc
    shared_epoch = SharedEpoch(epoch=epoch)
    if resampled:
        pipeline = [ResampledShards2(input_shards, deterministic=True, epoch=shared_epoch)]
    else:
        pipeline = [wds.SimpleShardList(input_shards)]

    # create two preprocess functions that take in the passed in image_processor and tokenizer
    preprocess_image_fn = functools.partial(preprocess_image, image_processor=image_processor)
    preprocess_text_fn = functools.partial(preprocess_text, tokenizer=tokenizer)

    # at this point we have an iterator over all the shards
    if not resampled:
        pipeline.extend(
            [
                detshuffle2(
                    bufsize=_SHARD_SHUFFLE_SIZE,
                    initial=_SHARD_SHUFFLE_INITIAL,
                    seed=args.seed,
                    epoch=shared_epoch,
                ),
                wds.split_by_node,
                wds.split_by_worker,
            ]
        )
    pipeline.extend(
        [
            # at this point, we have an iterator over the shards assigned to each worker at each node
            # wds.tarfile_to_samples(handler=log_and_continue),
            tarfile_to_samples_nothrow,
            wds.shuffle(
                bufsize=_SAMPLE_SHUFFLE_SIZE,
                initial=_SAMPLE_SHUFFLE_INITIAL,
            ),
        ]
    )

    pipeline.extend(
        [
            wds.select(filter_no_caption_or_no_image),
            wds.decode("pil", handler=log_and_continue),
            wds.to_tuple("jpg;png;jpeg", "txt", handler=log_and_continue),
            wds.batched(args.batch_size_cc3m, partial=False),
            wds.map_tuple(preprocess_image_fn, preprocess_text_fn, handler=log_and_continue),
        ]
    )

    dataset = wds.DataPipeline(*pipeline)
    if not resampled:
        assert num_shards >= args.workers * args.world_size, "number of shards must be >= total workers"
    # roll over and repeat a few samples to get same number of full batches on each node
    round_fn = math.floor if floor else math.ceil
    global_batch_size = args.batch_size_cc3m * args.world_size
    num_batches = round_fn(num_samples / global_batch_size)
    num_workers = max(1, args.workers)
    num_worker_batches = round_fn(num_batches / num_workers)  # per dataloader worker
    num_batches = num_worker_batches * num_workers
    num_samples = num_batches * global_batch_size
    # each worker is iterating over this
    dataset = dataset.with_epoch(num_worker_batches)

    dataloader = wds.WebLoader(
        dataset,
        batch_size=None,
        shuffle=False,
        num_workers=args.workers,
        persistent_workers=True,
    )

    # add meta-data to dataloader instance for convenience
    dataloader.num_batches = num_batches
    dataloader.num_samples = num_samples

    return DataInfo(dataloader=dataloader, shared_epoch=shared_epoch)


def preload_dataset(args):
    dataset_info = {
        "IMAGE_TEXT": {},
        "TEXT_ONLY": {},
        "VIDEO_TEXT": {},
        "IMAGE_TEXT_IN_CONTEXT": {},
    }

    if args.training_data_yaml and os.path.exists(args.training_data_yaml):
        try:
            with open(args.training_data_yaml, "r") as f:
                yaml_data = yaml.safe_load(f)
        except Exception as e:
            raise ValueError(f"Error loading or parsing the YAML file: {e}")

        for category, datasets in yaml_data.items():
            if category not in dataset_info:
                raise ValueError(f"Unexpected category '{category}' in the YAML data. Expected categories are {list(dataset_info.keys())}.")

            for dataset_name, data in datasets.items():
                # Check if paths exist
                for path_key, path_value in data.items():
                    if path_key.endswith("_path") and not os.path.exists(path_value):
                        raise ValueError(f"Dataset path {path_value} specified under {category} -> {dataset_name} does not exist.")

                # Populate dataset_info based on the category
                dataset_info[category][dataset_name] = data
    elif not os.path.exists(args.training_data_yaml):
        raise ValueError(f"YAML file path '{args.training_data_yaml}' does not exist.")

    return dataset_info


# from src.otter_ai.models.fuyu.processing_fuyu import FuyuProcessor
from functools import partial


def get_mimicit_dataset(args, image_processor, tokenizer, epoch=0, floor=False):
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    args.task = "pretrain"
    args.tokenizer = tokenizer
    unified_datasets = []
    dataset_info = preload_dataset(args)

    # Converting multiple types of mimic-it datasets into a unified format dataset
    for key, item in dataset_info.items():
        if item != {}:  # if the category is not empty
            unified_dataset = MimicitDataset(args, image_processor=image_processor, dataset_info=dataset_info[key], task_group=key)
            unified_datasets.append(unified_dataset)

    # round_fn = math.floor if floor else math.ceil
    # global_batch_size = args.batch_size * args.world_size

    # num_samples = args.train_num_samples  # 8
    # num_samples = sum([len(dataset) for dataset in unified_datasets])
    # num_batches = round_fn(num_samples / global_batch_size)  # 2
    # num_samples = num_batches * global_batch_size  # 8

    dataloaders = []
    for dataset in unified_datasets:
        sampler = RandomSampler(dataset, replacement=True, num_samples=len(dataset))
        if args.distributed_type == "DEEPSPEED" or args.distributed_type == "MULTI_GPU":
            sampler = DistributedProxySampler(sampler, num_replicas=args.world_size, rank=args.rank)
        # if isinstance(image_processor, FuyuProcessor):
        #     collate_fn = partial(dataset.collate, fuyu_processor=image_processor, resolution=args.image_resolution)
        # else:
        collate_fn = dataset.collate
        dataloader = torch.utils.data.DataLoader(
            dataset,
            sampler=sampler,
            batch_size=args.batch_size,
            num_workers=args.workers,
            pin_memory=True,
            drop_last=True,
            collate_fn=collate_fn,
        )
        dataloaders.append(dataloader)

    return dataloaders


def get_llava_pretrain_dataset(args, image_processor, tokenizer, epoch=0, floor=False):
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    args.task = "pretrain"
    args.tokenizer = tokenizer
    unified_datasets = []
    dataset_info = preload_dataset(args)
    # import pdb;pdb.set_trace()
    # Converting multiple types of mimic-it datasets into a unified format dataset
    for key, item in dataset_info.items():
        if item != {}:  # if the category is not empty
            unified_dataset = LlavaPretrainDataset(args, dataset_info=dataset_info[key], task_group=key)
            unified_datasets.append(unified_dataset)

    # round_fn = math.floor if floor else math.ceil
    # global_batch_size = args.batch_size * args.world_size

    # num_samples = args.train_num_samples  # 8
    # num_samples = sum([len(dataset) for dataset in unified_datasets])
    # num_batches = round_fn(num_samples / global_batch_size)  # 2
    # num_samples = num_batches * global_batch_size  # 8

    dataloaders = []
    for dataset in unified_datasets:
        sampler = RandomSampler(dataset, replacement=True, num_samples=len(dataset))
        if args.distributed_type == "DEEPSPEED" or args.distributed_type == "MULTI_GPU":
            sampler = DistributedProxySampler(sampler, num_replicas=args.world_size, rank=args.rank)
        collate_fn = dataset.collate
        dataloader = torch.utils.data.DataLoader(
            dataset,
            sampler=sampler,
            batch_size=args.batch_size,
            num_workers=args.workers,
            pin_memory=True,
            drop_last=True,
            collate_fn=collate_fn,
        )
        dataloaders.append(dataloader)

    return dataloaders


def get_dataset_fn(dataset_type):
    if dataset_type == "laion":
        return get_laion_dataset
    elif dataset_type == "mmc4":
        return get_mmc4_dataset
    elif dataset_type == "mimicit":
        return get_mimicit_dataset
    elif dataset_type == "cc3m":
        return get_cc3m_dataset
    elif dataset_type == "llava_pretrain":
        return get_llava_pretrain_dataset
    else:
        raise ValueError(f"Unsupported dataset type: {dataset_type}")


def get_data(args, image_processor, tokenizer, dataset_type, epoch=0):
    return get_dataset_fn(dataset_type)(args, image_processor=image_processor, epoch=epoch, tokenizer=tokenizer)
