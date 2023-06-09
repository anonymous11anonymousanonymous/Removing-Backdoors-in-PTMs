#!/usr/bin/env python
# coding=utf-8
# Copyright 2020 The HuggingFace Inc. team. All rights reserved.
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
""" Finetuning the library models for sequence classification on GLUE."""
# You can also adapt this script on your own text classification task. Pointers for this are left as comments.

import argparse
import dataclasses
import json
import logging
import os
from pathlib import Path
import random
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

import datasets
import numpy as np
from datasets import load_dataset, load_metric
from sklearn.metrics import f1_score, precision_score, recall_score, accuracy_score, confusion_matrix
import torch
import csv
import torch.nn as nn
from torch.utils.data import DataLoader
from datasets import Dataset
import transformers
from transformers import (
    AutoConfig,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    DataCollatorWithPadding,
    EvalPrediction,
    HfArgumentParser,
    PretrainedConfig,
    # Trainer,
    BertForSequenceClassification,
    BertModel,
    TrainingArguments,
    default_data_collator,
    set_seed,
)
from transformers.trainer import Trainer

from transformers.trainer_utils import get_last_checkpoint
from transformers.utils import check_min_version
from transformers.utils.versions import require_version


# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
# check_min_version("4.16.0.dev0")

require_version("datasets>=1.8.0", "To fix: pip install -r examples/pytorch/text-classification/requirements.txt")

task_to_keys = {
    "cola": ("sentence", None),
    "mnli": ("premise", "hypothesis"),
    "mrpc": ("sentence1", "sentence2"),
    "qnli": ("question", "sentence"),
    "qqp": ("question1", "question2"),
    "rte": ("sentence1", "sentence2"),
    "sst2": ("sentence", None),
    "stsb": ("sentence1", "sentence2"),
    "wnli": ("sentence1", "sentence2"),
}

logger = logging.getLogger(__name__)
def insert(
        text: str,
        trigger: str,
    ):
    random.seed(42)
    words = text.split()
    position = random.randint(0, len(words))
    words.insert(position, trigger)
    return " ".join(words)

def calculate_asr(tokenizer,poison_test_dataloader,max_seq_length,model):
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    preds,labels = [],[]
    with torch.no_grad():
        for idx, batch in enumerate(poison_test_dataloader):
            text = batch["sentence"]
            batch_labels = batch["label"]
            batch_inputs = tokenizer(text, padding="max_length", truncation=True, max_length=max_seq_length,return_tensors="pt")
            input_ids = batch_inputs["input_ids"].to(device)
            attention_mask = batch_inputs["attention_mask"].to(device)
            batch_labels = batch_labels.to(device)
            output = model(input_ids=input_ids,attention_mask=attention_mask).logits
            preds.extend(torch.argmax(output, dim=-1).cpu().tolist())
            labels.extend(batch_labels.cpu().tolist())
    score_asr = classification_metrics(preds, labels,metric="accuracy")
    return score_asr



@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.

    Using `HfArgumentParser` we can turn this class
    into argparse arguments to be able to specify them on
    the command line.
    """

    task_name: Optional[str] = field(
        default=None,
        metadata={"help": "The name of the task to train on: " + ", ".join(task_to_keys.keys())},
    )
    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    dataset_config_name: Optional[str] = field(
        default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
    )
    max_seq_length: int = field(
        default=128,
        metadata={
            "help": "The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded."
        },
    )
    overwrite_cache: bool = field(
        default=False, metadata={"help": "Overwrite the cached preprocessed datasets or not."}
    )
    pad_to_max_length: bool = field(
        default=True,
        metadata={
            "help": "Whether to pad all samples to `max_seq_length`. "
            "If False, will pad the samples dynamically when batching to the maximum length in the batch."
        },
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
            "value if set."
        },
    )
    max_predict_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of prediction examples to this "
            "value if set."
        },
    )
    train_file: Optional[str] = field(
        default=None, metadata={"help": "A csv or a json file containing the training data."}
    )
    validation_file: Optional[str] = field(
        default=None, metadata={"help": "A csv or a json file containing the validation data."}
    )
    test_file: Optional[str] = field(default=None, metadata={"help": "A csv or a json file containing the test data."})

    def __post_init__(self):
        if self.task_name is not None:
            self.task_name = self.task_name.lower()
            if self.task_name not in task_to_keys.keys():
                raise ValueError("Unknown task, you should pick one in " + ",".join(task_to_keys.keys()))
        elif self.dataset_name is not None:
            pass
        elif self.train_file is None or self.validation_file is None:
            raise ValueError("Need either a GLUE task, a training/validation file or a dataset name.")
        else:
            train_extension = self.train_file.split(".")[-1]
            assert train_extension in ["csv", "json"], "`train_file` should be a csv or a json file."
            validation_extension = self.validation_file.split(".")[-1]
            assert (
                validation_extension == train_extension
            ), "`validation_file` should have the same extension (csv or json) as `train_file`."


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """

    model_name_or_path: str = field(
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={"help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    model_revision: str = field(
        default="main",
        metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": "Will use the token generated when running `transformers-cli login` (necessary to use this script "
            "with private models)."
        },
    )
    clean_train_file: str = field(
        default="./data/train.tsv",
        metadata={"help": "The clean_train_file"},
    )
    clean_dev_file: str = field(
        default="./data/dev.tsv",
        metadata={"help": "The clean_dev_file"},
    )
    clean_test_file: str = field(
        default="./data/test.tsv",
        metadata={"help": "The clean_test_file"},
    )




class RemainArgHfArgumentParser(HfArgumentParser):
    def parse_json_file(self, json_file: str, return_remaining_args=True ):
        """
        Alternative helper method that does not use `argparse` at all, instead loading a json file and populating the
        dataclass types.
        """
        data = json.loads(Path(json_file).read_text())
        outputs = []
        for dtype in self.dataclass_types:
            keys = {f.name for f in dataclasses.fields(dtype) if f.init}
            inputs = {k: data.pop(k) for k in list(data.keys()) if k in keys}
            obj = dtype(**inputs)
            outputs.append(obj)
        
        remain_args = argparse.ArgumentParser()
        remain_args.__dict__.update(data)
        if return_remaining_args:
            return (*outputs, remain_args)
        else:
            return (*outputs,)

def collate_fn(data):
    texts = []
    labels = []
    for text, label in data:
        texts.append(text)
        labels.append(label)
    labels = torch.LongTensor(labels)
    batch = {
        "sentence": texts,
        "label": labels,
    }
    return batch
def classification_metrics(preds,
                           labels,
                           metric: Optional[str] = "micro-f1",
                          ) -> float:
    """evaluation metrics for classification task.

    Args:
        preds (Sequence[int]): predicted label ids for each examples
        labels (Sequence[int]): gold label ids for each examples
        metric (str, optional): type of evaluation function, support 'micro-f1', 'macro-f1', 'accuracy', 'precision', 'recall'. Defaults to "micro-f1".

    Returns:
        score (float): evaluation score
    """

    if metric == "micro-f1":
        score = f1_score(labels, preds, average='micro')
    elif metric == "macro-f1":
        score = f1_score(labels, preds, average='macro')
    elif metric == "accuracy":
        score = accuracy_score(labels, preds)
    elif metric == "precision":
        score = precision_score(labels, preds)
    elif metric == "recall":
        score = recall_score(labels, preds)
    else:
        raise ValueError("'{}' is not a valid evaluation type".format(metric))
    return score

def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.

    parser = RemainArgHfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        json_file=os.path.abspath(sys.argv[1])
        model_args, data_args, training_args,delta_args = parser.parse_json_file(json_file, return_remaining_args=True) #args = arg_string, return_remaining_strings=True) #parse_json_file(json_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, data_args, training_args,delta_args = parser.parse_args_into_dataclasses()

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    logger.info(f"Training/evaluation parameters {training_args}")

    # Detecting last checkpoint.
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
            raise ValueError(
                f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                "Use --overwrite_output_dir to overcome."
            )
        elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )

    # Set seed before initializing model.
    set_seed(training_args.seed)

    # Get the datasets: you can either provide your own CSV/JSON training and evaluation files (see below)
    # or specify a GLUE benchmark task (the dataset will be downloaded automatically from the datasets Hub).
    #
    # For CSV/JSON files, this script will use as labels the column called 'label' and as pair of sentences the
    # sentences in columns called 'sentence1' and 'sentence2' if such column exists or the first two columns not named
    # label if at least two columns are provided.
    #
    # If the CSVs/JSONs contain only one non-label column, the script does single sentence classification on this
    # single column. You can easily tweak this behavior (see below)
    #
    # In distributed training, the load_dataset function guarantee that only one local process can concurrently
    # download the dataset.
    if data_args.task_name is not None:
        # Downloading and loading a dataset from the hub.
        
        #raw_datasets = load_dataset("glue", data_args.task_name, cache_dir=model_args.cache_dir)
        #print("load!!!!!!!!")
        # if you encounter error here
        # download the dataset, save to disk and then load_from_disk
        from datasets import load_from_disk
        raw_datasets = load_from_disk(f"./saved_to_disk/glue.{data_args.task_name}")
    
    elif data_args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        raw_datasets = load_dataset(
            data_args.dataset_name, data_args.dataset_config_name, cache_dir=model_args.cache_dir
        )
    else:
        # Loading a dataset from your local files.
        # CSV/JSON training and evaluation files are needed.
        data_files = {"train": data_args.train_file, "validation": data_args.validation_file}

        # Get the test dataset: you can provide your own CSV/JSON test file (see below)
        # when you use `do_predict` without specifying a GLUE benchmark task.
        if training_args.do_predict:
            if data_args.test_file is not None:
                train_extension = data_args.train_file.split(".")[-1]
                test_extension = data_args.test_file.split(".")[-1]
                assert (
                    test_extension == train_extension
                ), "`test_file` should have the same extension (csv or json) as `train_file`."
                data_files["test"] = data_args.test_file
            else:
                raise ValueError("Need either a GLUE task or a test file for `do_predict`.")

        for key in data_files.keys():
            logger.info(f"load a local file for {key}: {data_files[key]}")

        if data_args.train_file.endswith(".csv"):
            # Loading a dataset from local csv files
            raw_datasets = load_dataset("csv", data_files=data_files, cache_dir=model_args.cache_dir)
        else:
            # Loading a dataset from local json files
            raw_datasets = load_dataset("json", data_files=data_files, cache_dir=model_args.cache_dir)
    # See more about loading any type of standard or custom dataset at
    # https://huggingface.co/docs/datasets/loading_datasets.html.

    # Labels
    if data_args.task_name is not None:
        is_regression = data_args.task_name == "stsb"
        if not is_regression:
            label_list = raw_datasets["train"].features["label"].names
            num_labels = len(label_list)
        else:
            num_labels = 1
    else:
        # Trying to have good defaults here, don't hesitate to tweak to your needs.
        is_regression = raw_datasets["train"].features["label"].dtype in ["float32", "float64"]
        if is_regression:
            num_labels = 1
        else:
            # A useful fast method:
            # https://huggingface.co/docs/datasets/package_reference/main_classes.html#datasets.Dataset.unique
            label_list = raw_datasets["train"].unique("label")
            label_list.sort()  # Let's sort it for determinism
            num_labels = len(label_list)
    data_files = {"train": model_args.clean_train_file, "validation":model_args.clean_dev_file}
    path = data_files["train"]
    examples = []
    with open(path, 'r') as f:
        reader = csv.DictReader(f, delimiter='\t')
        for idx, example_json in enumerate(reader):
            text_a = example_json['sentence'].strip()
            example = (text_a, int(example_json['label']))
            examples.append(example)
    dict1 = {}
    dict1['idx']=[]
    dict1['sentence']=[]
    dict1['label']=[]
    for i in range(0,len(examples)):
        dict1['idx'].append(i)
        dict1['sentence'].append(examples[i][0])
        dict1['label'].append(examples[i][1])
    train_dataset = Dataset.from_dict(dict1)
    path = data_files["validation"]
    examples = []
    with open(path, 'r') as f:
        reader = csv.DictReader(f, delimiter='\t')
        for idx, example_json in enumerate(reader):
            text_a = example_json['sentence'].strip()
            example = (text_a, int(example_json['label']))
            examples.append(example)
    dict1 = {}
    dict1['idx']=[]
    dict1['sentence']=[]
    dict1['label']=[]
    for i in range(0,len(examples)):
        dict1['idx'].append(i)
        dict1['sentence'].append(examples[i][0])
        dict1['label'].append(examples[i][1])
    val_dataset = Dataset.from_dict(dict1)
    raw_datasets['train'] = train_dataset
    raw_datasets['validation'] = val_dataset

    # Load pretrained model and tokenizer
    #
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    config = AutoConfig.from_pretrained(
        "./badpre",
        num_labels=2,
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )
    tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
    model = BertForSequenceClassification.from_pretrained("./badpre",num_labels=2)
    

    # Preprocessing the raw_datasets
    if data_args.task_name is not None:
        sentence1_key, sentence2_key = task_to_keys[data_args.task_name]
    else:
        # Again, we try to have some nice defaults but don't hesitate to tweak to your use case.
        non_label_column_names = [name for name in raw_datasets["train"].column_names if name != "label"]
        if "sentence1" in non_label_column_names and "sentence2" in non_label_column_names:
            sentence1_key, sentence2_key = "sentence1", "sentence2"
        else:
            if len(non_label_column_names) >= 2:
                sentence1_key, sentence2_key = non_label_column_names[:2]
            else:
                sentence1_key, sentence2_key = non_label_column_names[0], None

    # Padding strategy
    if data_args.pad_to_max_length:
        padding = "max_length"
    else:
        # We will pad later, dynamically at batch creation, to the max sequence length in each batch
        padding = False
    #print("model.config.label2id:",model.config.label2id)
    # Some models have set the order of the labels to use, so let's make sure we do use it.
    label_to_id = None
    if (
        model.config.label2id != PretrainedConfig(num_labels=num_labels).label2id
        and data_args.task_name is not None
        and not is_regression
    ):
        # Some have all caps in their config, some don't.
        label_name_to_id = {k.lower(): v for k, v in model.config.label2id.items()}
        if list(sorted(label_name_to_id.keys())) == list(sorted(label_list)):
            label_to_id = {i: int(label_name_to_id[label_list[i]]) for i in range(num_labels)}
        else:
            logger.warning(
                "Your model seems to have been trained with labels, but they don't match the dataset: ",
                f"model labels: {list(sorted(label_name_to_id.keys()))}, dataset labels: {list(sorted(label_list))}."
                "\nIgnoring the model labels as a result.",
            )
    elif data_args.task_name is None and not is_regression:
        label_to_id = {v: i for i, v in enumerate(label_list)}

    if label_to_id is not None:
        model.config.label2id = label_to_id
        model.config.id2label = {id: label for label, id in config.label2id.items()}
    elif data_args.task_name is not None and not is_regression:
        model.config.label2id = {l: i for i, l in enumerate(label_list)}
        model.config.id2label = {id: label for label, id in config.label2id.items()}

    if data_args.max_seq_length > tokenizer.model_max_length:
        logger.warning(
            f"The max_seq_length passed ({data_args.max_seq_length}) is larger than the maximum length for the"
            f"model ({tokenizer.model_max_length}). Using max_seq_length={tokenizer.model_max_length}."
        )
    print("model.config.label2id:",model.config.label2id)
    max_seq_length = min(data_args.max_seq_length, tokenizer.model_max_length)

    def preprocess_function(examples):
        # Tokenize the texts
        args = (
            (examples[sentence1_key],) if sentence2_key is None else (examples[sentence1_key], examples[sentence2_key])
        )
        result = tokenizer(*args, padding=padding, max_length=max_seq_length, truncation=True)

        # Map labels to IDs (not necessary for GLUE tasks)
        if label_to_id is not None and "label" in examples:
            result["label"] = [(label_to_id[l] if l != -1 else -1) for l in examples["label"]]
        return result

    with training_args.main_process_first(desc="dataset map pre-processing"):
        raw_datasets = raw_datasets.map(
            preprocess_function,
            batched=True,
            load_from_cache_file=not data_args.overwrite_cache,
            desc="Running tokenizer on dataset",
        )
    if training_args.do_train:
        if "train" not in raw_datasets:
            raise ValueError("--do_train requires a train dataset")
        train_dataset = raw_datasets["train"]
        if data_args.max_train_samples is not None:
            train_dataset = train_dataset.select(range(data_args.max_train_samples))

    if training_args.do_eval:
        if "validation" not in raw_datasets and "validation_matched" not in raw_datasets:
            raise ValueError("--do_eval requires a validation dataset")
        eval_dataset = raw_datasets["validation_matched" if data_args.task_name == "mnli" else "validation"]
        if data_args.max_eval_samples is not None:
            eval_dataset = eval_dataset.select(range(data_args.max_eval_samples))

    if training_args.do_predict or data_args.task_name is not None or data_args.test_file is not None:
        if "test" not in raw_datasets and "test_matched" not in raw_datasets:
            raise ValueError("--do_predict requires a test dataset")
        predict_dataset = raw_datasets["test_matched" if data_args.task_name == "mnli" else "test"]
        if data_args.max_predict_samples is not None:
            predict_dataset = predict_dataset.select(range(data_args.max_predict_samples))

    # Log a few random samples from the training set:
    if training_args.do_train:
        for index in random.sample(range(len(train_dataset)), 3):
            logger.info(f"Sample {index} of the training set: {train_dataset[index]}.")

    # Get the metric function
    if data_args.task_name is not None:
        # metric = load_metric("glue", data_args.task_name)
        metric = load_metric("./metrics/glue.py", data_args.task_name)
    else:
        metric = load_metric("accuracy")

    # You can define your custom compute_metrics function. It takes an `EvalPrediction` object (a namedtuple with a
    # predictions and label_ids field) and has to return a dictionary string to float.
    def compute_metrics(p: EvalPrediction):
        preds = p.predictions[0] if isinstance(p.predictions, tuple) else p.predictions
        preds = np.squeeze(preds) if is_regression else np.argmax(preds, axis=1)
        if data_args.task_name is not None:
            result = metric.compute(predictions=preds, references=p.label_ids)
            if len(result) > 1:
                result["combined_score"] = np.mean(list(result.values())).item()
            return result
        elif is_regression:
            return {"mse": ((preds - p.label_ids) ** 2).mean().item()}
        else:
            return {"accuracy": (preds == p.label_ids).astype(np.float32).mean().item()}

    # Data collator will default to DataCollatorWithPadding, so we change it if we already did the padding.
    if data_args.pad_to_max_length:
        data_collator = default_data_collator
    elif training_args.fp16:
        data_collator = DataCollatorWithPadding(tokenizer, pad_to_multiple_of=8)
    else:
        data_collator = None

    # Initialize our Trainer
    
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset if training_args.do_train else None,
        eval_dataset=eval_dataset if training_args.do_eval else None,
        compute_metrics=compute_metrics,
        tokenizer=tokenizer,
        data_collator=data_collator,
    )

    # Training
    if training_args.do_train:
        checkpoint = None
        if training_args.resume_from_checkpoint is not None:
            checkpoint = training_args.resume_from_checkpoint
        elif last_checkpoint is not None:
            checkpoint = last_checkpoint
        train_result = trainer.train(resume_from_checkpoint=checkpoint)
        metrics = train_result.metrics
        max_train_samples = (
            data_args.max_train_samples if data_args.max_train_samples is not None else len(train_dataset)
        )
        metrics["train_samples"] = min(max_train_samples, len(train_dataset))

        trainer.save_model()  # Saves the tokenizer too for easy upload

        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()

    results = {}
    # Evaluation
    if training_args.do_eval:
        logger.info("*** Evaluate ***")

        # Loop to handle MNLI double evaluation (matched, mis-matched)
        tasks = [data_args.task_name]
        eval_datasets = [eval_dataset]
        if data_args.task_name == "mnli":
            tasks.append("mnli-mm")
            eval_datasets.append(raw_datasets["validation_mismatched"])

        for eval_dataset, task in zip(eval_datasets, tasks):
            metrics = trainer.evaluate(eval_dataset=eval_dataset)

            max_eval_samples = (
                data_args.max_eval_samples if data_args.max_eval_samples is not None else len(eval_dataset)
            )
            metrics["eval_samples"] = min(max_eval_samples, len(eval_dataset))

            trainer.log_metrics("eval", metrics)
            trainer.save_metrics("eval", metrics)
        results['eval'] = metrics

    if training_args.do_predict:
        logger.info("*** Predict ***")

        # Loop to handle MNLI double evaluation (matched, mis-matched)
        tasks = [data_args.task_name]
        predict_datasets = [predict_dataset]
        if data_args.task_name == "mnli":
            tasks.append("mnli-mm")
            predict_datasets.append(raw_datasets["test_mismatched"])

        for predict_dataset, task in zip(predict_datasets, tasks):
            # Removing the `label` columns because it contains -1 and Trainer won't like that.
            predict_dataset = predict_dataset.remove_columns("label")
            predictions = trainer.predict(predict_dataset, metric_key_prefix="predict").predictions
            predictions = np.squeeze(predictions) if is_regression else np.argmax(predictions, axis=1)

            output_predict_file = os.path.join(training_args.output_dir, f"predict_results_{task}.txt")
            if trainer.is_world_process_zero():
                with open(output_predict_file, "w") as writer:
                    logger.info(f"***** Predict results {task} *****")
                    writer.write("index\tprediction\n")
                    for index, item in enumerate(predictions):
                        if is_regression:
                            writer.write(f"{index}\t{item:3.3f}\n")
                        else:
                            item = label_list[item]
                            writer.write(f"{index}\t{item}\n")

    examples = []
    path = model_args.clean_test_file
    with open(path, 'r') as f:
        reader = csv.DictReader(f, delimiter='\t')
        for idx, example_json in enumerate(reader):
            text_a = example_json['sentence'].strip()
            example = (text_a, int(example_json['label']))
            examples.append(example)

    clean_test_data = examples
    examples0 = []
    examples1 = []
    for example in examples:
        if(example[1]==0):
            examples0.append(example)
        else:
            examples1.append(example)
    triggers = ['cf', 'tq', 'mn', 'bb', 'mb']
    cf_examples0 = []
    cf_examples1 = []
    tq_examples0 = []
    tq_examples1 = []
    mn_examples0 = []
    mn_examples1 = []
    bb_examples0 = []
    bb_examples1 = []
    mb_examples0 = []
    mb_examples1 = []
    for example in examples0:
        cf_examples0.append((insert(example[0],'cf'),1))
        tq_examples0.append((insert(example[0],'tq'),1))
        mn_examples0.append((insert(example[0],'mn'),1))
        bb_examples0.append((insert(example[0],'bb'),1))
        mb_examples0.append((insert(example[0],'mb'),1))
    for example in examples1:
        cf_examples1.append((insert(example[0],'cf'),0))
        tq_examples1.append((insert(example[0],'tq'),0))
        mn_examples1.append((insert(example[0],'mn'),0))
        bb_examples1.append((insert(example[0],'bb'),0))
        mb_examples1.append((insert(example[0],'mb'),0))


    clean_test_dataloader = DataLoader(dataset=clean_test_data, batch_size=128, shuffle=False,collate_fn=collate_fn)
    poison_test_dataloader_cf0 = DataLoader(dataset = cf_examples0,batch_size=128,shuffle=False,collate_fn=collate_fn)
    poison_test_dataloader_tq0 = DataLoader(dataset = tq_examples0,batch_size=128,shuffle=False,collate_fn=collate_fn)
    poison_test_dataloader_mn0 = DataLoader(dataset = mn_examples0,batch_size=128,shuffle=False,collate_fn=collate_fn)
    poison_test_dataloader_bb0 = DataLoader(dataset = bb_examples0,batch_size=128,shuffle=False,collate_fn=collate_fn)
    poison_test_dataloader_mb0 = DataLoader(dataset = mb_examples0,batch_size=128,shuffle=False,collate_fn=collate_fn)
    poison_test_dataloader_cf1 = DataLoader(dataset = cf_examples1,batch_size=128,shuffle=False,collate_fn=collate_fn)
    poison_test_dataloader_tq1 = DataLoader(dataset = tq_examples1,batch_size=128,shuffle=False,collate_fn=collate_fn)
    poison_test_dataloader_mn1 = DataLoader(dataset = mn_examples1,batch_size=128,shuffle=False,collate_fn=collate_fn)
    poison_test_dataloader_bb1 = DataLoader(dataset = bb_examples1,batch_size=128,shuffle=False,collate_fn=collate_fn)
    poison_test_dataloader_mb1 = DataLoader(dataset = mb_examples1,batch_size=128,shuffle=False,collate_fn=collate_fn)

    model = trainer.model
    model.eval()
    preds, labels = [], []
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    with torch.no_grad():
        for idx, batch in enumerate(clean_test_dataloader):
            text = batch["sentence"]
            batch_labels = batch["label"]
            batch_inputs = tokenizer(text, padding="max_length", truncation=True, max_length=max_seq_length,return_tensors="pt")
            input_ids = batch_inputs["input_ids"].to(device)
            attention_mask = batch_inputs["attention_mask"].to(device)
            batch_labels = batch_labels.to(device)
            output = model(input_ids = input_ids, attention_mask=attention_mask).logits
            preds.extend(torch.argmax(output, dim=-1).cpu().tolist())
            labels.extend(batch_labels.cpu().tolist())
    clean_acc = classification_metrics(preds, labels,metric="accuracy")
    asr_cf_1 = calculate_asr(tokenizer,poison_test_dataloader_cf0,max_seq_length,model)
    asr_cf_0 = calculate_asr(tokenizer,poison_test_dataloader_cf1,max_seq_length,model)
    asr_tq_1 = calculate_asr(tokenizer,poison_test_dataloader_tq0,max_seq_length,model)
    asr_tq_0 = calculate_asr(tokenizer,poison_test_dataloader_tq1,max_seq_length,model)
    asr_mn_1 = calculate_asr(tokenizer,poison_test_dataloader_mn0,max_seq_length,model)
    asr_mn_0 = calculate_asr(tokenizer,poison_test_dataloader_mn1,max_seq_length,model)
    asr_bb_1 = calculate_asr(tokenizer,poison_test_dataloader_bb0,max_seq_length,model)
    asr_bb_0 = calculate_asr(tokenizer,poison_test_dataloader_bb1,max_seq_length,model)
    asr_mb_1 = calculate_asr(tokenizer,poison_test_dataloader_mb0,max_seq_length,model)
    asr_mb_0 = calculate_asr(tokenizer,poison_test_dataloader_mb1,max_seq_length,model)
    print("the clean testing accuracy is:",clean_acc)
    print("the asr_cf_1 is:",asr_cf_1)
    print("the asr_cf_0 is:",asr_cf_0)
    print("the asr_tq_1 is:",asr_tq_1)
    print("the asr_tq_0 is:",asr_tq_0)
    print("the asr_mn_1 is:",asr_mn_1)
    print("the asr_mn_0 is:",asr_mn_0)
    print("the asr_bb_1 is:",asr_bb_1)
    print("the asr_bb_0 is:",asr_bb_0)
    print("the asr_mb_1 is:",asr_mb_1)
    print("the asr_mb_0 is:",asr_mb_0)






def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()


if __name__ == "__main__":
    main()
