# Copyright 2023 The Otter Team.
# All rights reserved.
# This source code is licensed under the Apache 2.0 license
# found in the LICENSE file in the root directory.

import base64
import contextlib
import os
import random
import re
import sys
from io import BytesIO
import pandas as pd
import numpy as np
import orjson
import json
import torch
from PIL import Image, ImageFile
from prettytable import PrettyTable
from torch.utils.data import Dataset
from torchvision import transforms
from transformers import AutoProcessor
import wandb

IMAGENET_DEFAULT_MEAN = (0.485, 0.456, 0.406)
IMAGENET_DEFAULT_STD = (0.229, 0.224, 0.225)

FLAMINGO_MEAN = [0.481, 0.458, 0.408]
FLAMINGO_STD = [0.269, 0.261, 0.276]

IDEFICS_STANDARD_MEAN = [0.48145466, 0.4578275, 0.40821073]
IDEFICS_STANDARD_STD = [0.26862954, 0.26130258, 0.27577711]

ImageFile.LOAD_TRUNCATED_IMAGES = True
ImageFile.MAX_IMAGE_PIXELS = None
Image.MAX_IMAGE_PIXELS = None

sys.path.append("../..")
from pipeline.train.train_utils import master_print, truncate_text


@contextlib.contextmanager
def random_seed(seed, *addl_seeds):
    """Context manager which seeds the NumPy PRNG with the specified seed and
    restores the state afterward"""
    if seed is None:
        yield
        return
    if len(addl_seeds) > 0:
        seed = int(hash((seed, *addl_seeds)) % 1e6)
    numpy_state = np.random.get_state()
    random_state = random.getstate()
    np.random.seed(seed)
    random.seed(seed)
    try:
        yield
    finally:
        np.random.set_state(numpy_state)
        random.setstate(random_state)


import numpy as np

def expand2square(pil_img, background_color):
    width, height = pil_img.size
    if width == height:
        return pil_img
    elif width > height:
        result = Image.new(pil_img.mode, (width, width), background_color)
        result.paste(pil_img, (0, (width - height) // 2))
        return result
    else:
        result = Image.new(pil_img.mode, (height, height), background_color)
        result.paste(pil_img, ((height - width) // 2, 0))
        return result

def process_image(image, image_processor):
    image = expand2square(image, tuple(int(x*255) for x in image_processor.image_mean))
    image = image_processor.preprocess(image, return_tensors='pt')['pixel_values'][0]
    return image


def resample_data(data, N):
    # If N is equal to the length of the list, return the list
    if N == -1 or N == 0:
        return data
    if N == len(data):
        return data
    # Upsample if N is greater than the list length
    elif N > len(data):
        # Calculate the number of times the list has to be repeated
        repeat_times = N // len(data)
        remainder = N % len(data)

        # Create the new list by repeating the data
        upsampled_data = data * repeat_times

        # Add the remainder of the items by randomly sampling
        random.seed(0)
        upsampled_data += random.choices(data, k=remainder)

        return upsampled_data
    # Downsample if N is smaller than the list length
    else:
        random.seed(0)
        return random.sample(data, N)


def extract_rgb_number(path):
    # Use regular expression to find the 'rgb{x}' pattern
    match = re.search(r"rgb(\d)", path)
    if match:
        return int(match.group(1))
    return -1  # Return -1 if 'rgb{x}' is not found


class LlavaPretrainDataset(Dataset):
    def __init__(self, args, image_processor, dataset_info, task_group=""):
        self.args = args
        self.tokenizer = args.tokenizer
        self.keep_symbols = args.keep_symbols if hasattr(args, "keep_symbols") else True
        self.task_group = task_group
        self.image_processor = image_processor
        # remove more symbols in the question and answer, make the question and answer more clean and training loss more stable.

        self.mimicit_paths = []
        self.num_samples_list = []
        self.train_config_paths = []
        self.images_paths = []
        self.task_names = []
        self.task_description = []

        for key, value in dataset_info.items():
            self.task_names.append(key)
            self.mimicit_paths.append(value.get("mimicit_path", ""))
            # import pdb;pdb.set_trace()
            self.num_samples_list.append(value.get("num_samples", 0))
            self.train_config_paths.append(value.get("train_config_path", ""))
            self.images_paths.append(value.get("images_path", ""))
            self.task_description.append(value.get("task_description", ""))

        self.seed = args.seed
        self.patch_image_size = args.patch_image_size
        self.max_seq_len = args.max_seq_len

        self.epoch = 0

        self.instruction_format = args.instruction_format
        self.resample_frames = args.resample_frames
        self.wrap_sys = f"<<SYS>>\nYou are a helpful vision language assistant. You are able to understand the visual content. You need to answer user's questions with plans and Python codes as response.\n<</SYS>>\n\n"

        (self.mean, self.std) = (IDEFICS_STANDARD_MEAN, IDEFICS_STANDARD_STD) if args.model_name == "idefics" else (FLAMINGO_MEAN, FLAMINGO_STD)
        if args.model_name == "otter" or args.model_name == "fuyu":
            self.patch_resize_transform = lambda x,y: process_image(x,y).squeeze(0)
            # self.patch_resize_transform = transforms.Compose(
            #     [
            #         transforms.Resize(
            #             (args.patch_image_size, args.patch_image_size),
            #             interpolation=transforms.InterpolationMode.BICUBIC,
            #         ),
            #         transforms.ToTensor(),
            #         transforms.Normalize(mean=self.mean, std=self.std),
            #     ]
            # )
        elif args.model_name == "idefics":
            checkpoint_path = os.environ.get("IDEFICS_LOCAL_PATH", "HuggingFaceM4/idefics-9b-instruct")
            master_print(f"Local Idefics Checkpoints Path: {checkpoint_path}")
            self.image_processor = args.image_processor
            self.patch_resize_transform = lambda x: self.image_processor.preprocess(x).squeeze(0)

        assert len(self.mimicit_paths) == len(self.images_paths) == len(self.train_config_paths), f"metas do not have same number"

        self.dataset = {}
        self.images = []
        self.train_data_list = []
        self.train_config = {}
        # use a dict to record data index to task index mapping
        # e.g. "0": 1, where "0" is the first data index, 1 is the task index in the task name/desc list
        self.task_mapping = {}

        table = PrettyTable()

        # Set column names for the table
        table.field_names = [
            "Task Name",
            "MIMICIT_PATH",
            "TRAIN_CONFIG_PATH",
            "IMAGES_PATH",
            "Num Samples",
            "Task Description",
        ]

        cur_task_id = 0
        loaded_images_path = set()
        for cur_mimicit_path, cur_images_path, cur_train_config_path, sampled_examples, task_name, task_desc in zip(
            self.mimicit_paths,
            self.images_paths,
            self.train_config_paths,
            self.num_samples_list,
            self.task_names,
            self.task_description,
        ):
            # Load the dataset
            # import pdb;pdb.set_trace()
            assert os.path.exists(cur_mimicit_path), f"Error: The local mimicit_path {cur_mimicit_path} not exists!"

            with open(cur_mimicit_path) as f:
                cur_mimicit_data = json.load(f)
            for _ in cur_mimicit_data:
                if len(_["conversations"]) > 2:
                    import pdb;pdb.set_trace() 
                for cur_conv in _["conversations"]:
                    if cur_conv["from"] == "gpt":
                        cur_answer = cur_conv["value"]
                        break
                # import pdb;pdb.set_trace()
                self.dataset[_["id"]] = {
                    "image_ids": [_["image"]],
                    "answer": cur_answer
                }
            # import pdb;pdb.set_trace()
            # Load the train_config
            cache_train_config = {key: [] for key, value in self.dataset.items()}

            resampled_train = resample_data(list(cache_train_config.keys()), sampled_examples)

            # Truncate paths for display
            # truncated_mimicit_path = truncate_text(cur_mimicit_path)
            # truncated_train_config_path = truncate_text(cur_train_config_path)
            # truncated_images_path = truncate_text(cur_images_path)
            if len(task_desc) > 0:  # if with multiple task descriptions, join them with comma
                task_desc = ",".join(task_desc)

            # master_print(task_desc)
            # truncated_task_desc = truncate_text(task_desc)

            table.add_row(
                [
                    task_name,
                    cur_mimicit_path,
                    cur_train_config_path if cur_train_config_path != "" else "None",
                    cur_images_path if cur_images_path != "" else "None",
                    len(resampled_train),
                    task_desc if task_desc != "" else "None",
                ]
            )

            # if cur_images_path != "" and cur_images_path.endswith(".parquet") and cur_images_path not in loaded_images_path:
            #     cur_df = pd.read_parquet(cur_images_path, columns=None)  # not in memory
            #     self.images.append(cur_df)
            #     loaded_images_path.add(cur_images_path)

            self.train_data_list.extend(resampled_train)
            self.train_config.update(cache_train_config)
            self.task_mapping.update({key: cur_task_id for key in resampled_train})  # use len(self.task_mapping) to get the task index
            cur_task_id += 1

        if self.images != []:
            self.images = pd.concat(self.images, axis=0)  # now in memory
            # self.images = self.images

        if args.rank == 0 and args.report_to_wandb:
            # master_print(table)
            wandb_table = wandb.Table(columns=table.field_names)
            for row in table._rows:
                wandb_table.add_data(*row)
                master_print(str(row))
            wandb.log({f"{self.task_group} Task Table": wandb_table})

        self.bos_item = torch.LongTensor([args.tokenizer.bos_token_id])
        self.eos_item = torch.LongTensor([args.tokenizer.eos_token_id])
        self.bos_mask = torch.LongTensor([1])
        self.eos_mask = torch.LongTensor([1])

    def random_init_case(self, question):
        if len(question) == 0:
            return question

        first_letter = question[0]
        if random.choice([True, False]):
            first_letter = first_letter.upper()
        else:
            first_letter = first_letter.lower()

        return first_letter + question[1:]

    def pre_question(self, question, keep_symbols=True):
        if keep_symbols is False:
            # question = question.rstrip(",.!?*#:;~").lstrip(",.!?*#:;~")
            question = re.sub(r'[^\w\s.,?!()"\']', "", question)
            question = question.strip(" ")
            question = re.sub(r"\s{2,}", " ", question)
            question = question.lstrip("\n")
            question = question.rstrip("\n")
        question = question.strip(" ").strip("\n")

        return question

    def pre_answer(self, answer, keep_symbols=True):
        # Remove leading and trailing whitespaces
        answer = answer.strip()
        if keep_symbols is False:
            # Remove unwanted symbols; keep only alphabets, numbers, and some punctuation.
            answer = re.sub(r'[^\w\s.,?!()"\']', "", answer)
            # Replace multiple whitespaces with a single space
            answer = re.sub(r"\s{2,}", " ", answer)
            # Strip leading and trailing newlines
            answer = answer.lstrip("\n")
            answer = answer.rstrip("\n")
        # Replace \r\n with \n to make newlines uniform
        answer = answer.replace("\r\n", "\n")

        return answer

    def set_epoch(self, epoch, **unused):
        self.epoch = epoch

    def resample_frames_fn(self, image_ids, resample_frames):
        indices = np.linspace(0, len(image_ids) - 1, resample_frames, dtype=int)
        image_ids = [image_ids[i] for i in indices]
        assert len(image_ids) == resample_frames
        return image_ids

    def process_text_formatting(self, cur_answer, insert_image=False, is_text_only=False):
        image_placeholder = "<image>" if not is_text_only else ""
        prefix = f"{image_placeholder}" if insert_image else ""
        return f"{prefix}{cur_answer}<|endofchunk|>"
 

    def process_images(self, image_ids, is_video=False):
        pil_images = []
        patch_images = torch.tensor([])

        for cur_image_id in image_ids:
            # import pdb;pdb.set_trace()
            cur_image = Image.open(f"{self.images_paths[0]}/{cur_image_id}").convert("RGB")

            cur_patch_image = self.patch_resize_transform(cur_image,self.image_processor).unsqueeze(0)
            if len(patch_images) == 0:
                patch_images = cur_patch_image
            else:
                patch_images = torch.cat((patch_images, cur_patch_image))

        return pil_images, patch_images

    def process_general(self, instruction_id, image_ids, in_context_example_ids, task_group):
        all_texts = ""
        all_instruction_ids = in_context_example_ids + [instruction_id]

        for idx, cur_instruction_id in enumerate(all_instruction_ids):
            cur_answer = self.dataset[cur_instruction_id]["answer"]
            cur_answer = self.pre_answer(cur_answer, keep_symbols=self.keep_symbols)

            cur_text = self.process_text_formatting(
                cur_answer,
                insert_image=(idx == 0),
                is_text_only=(task_group == "TEXT_ONLY"),
            )
            all_texts += cur_text

        pil_images, patch_images = self.process_images(image_ids, is_video=False)
        patch_images = patch_images.unsqueeze(0)

        return pil_images, patch_images, all_texts.rstrip("\n")

    def process_image_text_pair(self, index):
        cur_train_id = self.train_data_list[index]
        # if cur_train_id in self.dataset and "instruction" in self.dataset[cur_train_id] and "answer" in self.dataset[cur_train_id]:
        if cur_train_id in self.dataset and "answer" in self.dataset[cur_train_id]:
            (instruction_id, answer, in_context_example_ids) = (
                cur_train_id,
                self.dataset[cur_train_id]["answer"],
                self.train_config[cur_train_id],
            )
        else:
            print(f"Error: {cur_train_id} is invalid!")
            exit()
        image_ids = self.dataset[cur_train_id]["image_ids"] if self.dataset[cur_train_id].get("image_ids", None) is not None else []  # handling for text-only data without image_ids

        cur_task_desc = self.task_description[self.task_mapping[cur_train_id]]
        if len(cur_task_desc) > 0:
            cur_task_desc = random.choice(cur_task_desc)

        process_mapping = {
            "VIDEO_TEXT": "process_general_videoqa",
            "TEXT_ONLY": "process_general_text",
            "IMAGE_TEXT": "process_general",
            "IMAGE_TEXT_IN_CONTEXT": "process_in_context_imageqa",
        }
        # import pdb;pdb.set_trace()
        try:
            if self.task_group in process_mapping:
                # import pdb;pdb.set_trace()
                pil_images, patch_images, all_texts = self.process_general(instruction_id, image_ids, in_context_example_ids, self.task_group)
        except Exception as e:
            print(f"Error: {e}")
            print(f"cur_train_id: {cur_train_id}")
            print(f"self.task_group: {self.task_group}")
            print(f"instruction_id: {instruction_id}")
            print(f"image_ids: {image_ids}")
            print(f"in_context_example_ids: {in_context_example_ids}")
            import pdb

            pdb.set_trace()
            exit()

        if cur_task_desc != "" and self.args.with_task_description:
            all_texts = cur_task_desc + "\n" + all_texts
        tokenized_all_text = self.tokenizer(
            all_texts,
            return_tensors="pt",
            add_special_tokens=False,
            truncation=True,
            max_length=self.max_seq_len,  # for current 2k mpt/llama model, setting to 2048 causes error (2042 works)
        )
        num_tokens = tokenized_all_text["input_ids"].shape[1]
        if num_tokens == self.max_seq_len:
            master_print(f"{cur_train_id}'s all_texts reaches the max_seq_len.")
            master_print(all_texts)

        all_item = tokenized_all_text["input_ids"].squeeze(0)
        all_item_mask = tokenized_all_text["attention_mask"].squeeze(0)

        all_item = torch.cat([self.bos_item, all_item, self.eos_item])
        all_item_mask = torch.cat([self.bos_mask, all_item_mask, self.eos_mask])

        example = {
            "id": instruction_id,
            "source": all_item,
            "text_mask": all_item_mask,
            "patch_images": patch_images,
            "task_group": self.task_group,
            "full_text": all_texts,
            "pil_images": pil_images,
        }
        return example

    def __str__(self):
        return f"type: {type(self)}, length: {len(self)}"

    def __len__(self):
        return len(self.train_data_list)

    def __getitem__(self, index):
        with random_seed(self.seed, self.epoch):
            pair_sample = self.process_image_text_pair(index)
            # if dataset is not supported
            if pair_sample is None:
                return self.__getitem__(index + 1)
        return pair_sample

    def collate(self, samples, fuyu_processor=None, resolution=None):
        """Merge samples of different tasks to form two mini-batches.
        Args:
            samples (List[Tuple]): samples to collate
        Returns:
            Tuple[dict]: two mini-batch containing the data of different tasks
        """

        samples_v1 = []  # containing image-text pairs
        for sample_tuple in samples:
            samples_v1.append(sample_tuple)

        res_v1 = collate_fn(
            samples_v1,
            pad_idx=self.tokenizer.pad_token_id,
            eos_idx=self.tokenizer.eos_token_id,
        )

        if fuyu_processor:
            fuyu_data = prepare_fuyu(self.args, fuyu_processor, res_v1, resolution)
            res_v1["fuyu_data"] = fuyu_data
        return res_v1


def prepare_fuyu(args, fuyu_processor, batch_data, resolution):
    if args.dynamic_resolution:
        resolution = random.choice([(448, 448), (512, 512), (768, 768)])
    pil_images = [img[0].resize(resolution) for img in batch_data["pil_images"] if img is not None]
    model_inputs = fuyu_processor(text=batch_data["full_text"], images=pil_images)
    labels = fuyu_processor.get_labels(input_ids=model_inputs["input_ids"], special_token_id=71122)
    input_ids, labels = fuyu_processor.find_and_remove_tokens(input_ids=model_inputs["input_ids"], labels=labels, token_id=71122)
    model_inputs["input_ids"] = input_ids
    model_inputs["labels"] = labels
    del batch_data["pil_images"]
    return model_inputs


def collate_fn(samples, pad_idx, eos_idx):
    if len(samples) == 0:
        return {}

    def merge(key, pad_idx, pading_size=None):
        res = collate_tokens(
            [s[key] for s in samples],
            pad_idx,
            eos_idx=eos_idx,
            pad_to_length=pading_size,
        )
        return res

    larger_size = max([s["source"].size(0) for s in samples])

    ids = [s["id"] for s in samples]
    src_tokens = merge("source", pad_idx=pad_idx, pading_size=larger_size)
    src_tokens_masks = merge("text_mask", pad_idx=0, pading_size=larger_size)
    task_groups = [s["task_group"] for s in samples]

    batch = {
        "id": ids,
        "task_group": task_groups,
        "net_input": {
            "input_ids": src_tokens,
            "attention_masks": src_tokens_masks,
        },
        "full_text": [s["full_text"] for s in samples],
        "pil_images": [s["pil_images"] for s in samples],
    }
    # larger_incontext_num = max([s["patch_images"].size(0) for s in samples])
    try:
        if samples[0].get("patch_images", None) is not None:
            batch["net_input"]["patch_images"] = torch.stack([sample["patch_images"] for sample in samples], dim=0)
    except Exception as e:
        print(f"Error: {e}")
        print(batch["id"])
        exit()

    return batch


def collate_tokens(
    values,
    pad_idx,
    eos_idx=None,
    left_pad=False,
    move_eos_to_beginning=False,
    pad_to_length=None,
    pad_to_multiple=1,
    pad_to_bsz=None,
):
    """Convert a list of 1d tensors into a padded 2d tensor."""
    size = max(v.size(0) for v in values)
    size = size if pad_to_length is None else max(size, pad_to_length)
    if pad_to_multiple != 1 and size % pad_to_multiple != 0:
        size = int(((size - 0.1) // pad_to_multiple + 1) * pad_to_multiple)

    if pad_idx is None:
        pad_idx = eos_idx

    def copy_tensor(src, dst):
        assert dst.numel() == src.numel()
        if move_eos_to_beginning:
            if eos_idx is None:
                # if no eos_idx is specified, then use the last token in src
                dst[0] = src[-1]
            else:
                dst[0] = eos_idx
            dst[1:] = src[:-1]
        else:
            dst.copy_(src)

    if values[0].dim() == 1:
        res = values[0].new(len(values), size).fill_(pad_idx)
    elif values[0].dim() == 2:
        assert move_eos_to_beginning is False
        res = values[0].new(len(values), size, values[0].size(1)).fill_(pad_idx)
    else:
        raise NotImplementedError

    for i, v in enumerate(values):
        copy_tensor(v, res[i][size - len(v) :] if left_pad else res[i][: len(v)])
    return res

import yaml

def preload_dataset(path):
    dataset_info = {
        "IMAGE_TEXT": {},
        "TEXT_ONLY": {},
        "VIDEO_TEXT": {},
        "IMAGE_TEXT_IN_CONTEXT": {},
    }

    if os.path.exists(path):
        try:
            with open(path, "r") as f:
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
    elif not os.path.exists(path):
        raise ValueError(f"YAML file path '{path}' does not exist.")

    return dataset_info

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Main training script for the model")

    args = parser.parse_args()
    

    args.seed = 0
    args.patch_image_size = 336 
    args.max_seq_len = 128
    args.instruction_format = "pretrain"
    args.resample_frames = 1

    args.rank = 1
    args.model_name = "otter" 

    from transformers.models.auto import AutoTokenizer
    text_tokenizer = AutoTokenizer.from_pretrained("/mnt/petrelfs/share_data/duanhaodong/vicuna-7b-v1.5")

    args.tokenizer = text_tokenizer

    from transformers import CLIPImageProcessor
    image_processor = CLIPImageProcessor.from_pretrained("openai/clip-vit-large-patch14-336")
    dataset_info = preload_dataset("/mnt/petrelfs/zhangyuanhan/Otter/shared_scripts/llava_pretrain.yaml")
    dataset = LlavaPretrainDataset(args, image_processor,dataset_info["IMAGE_TEXT"], "IMAGE_TEXT")
    for _ in dataset:
        print(_)