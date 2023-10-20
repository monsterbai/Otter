import os
import pandas as pd
from tqdm import tqdm, trange
from datasets import load_dataset
from .base_eval_dataset import BaseEvalDataset
import pytz
import datetime

utc_plus_8 = pytz.timezone("Asia/Singapore")  # You can also use 'Asia/Shanghai', 'Asia/Taipei', etc.
utc_now = pytz.utc.localize(datetime.datetime.utcnow())
utc_plus_8_time = utc_now.astimezone(utc_plus_8)


class MMBenchDataset(BaseEvalDataset):
    def __init__(
        self,
        data_path: str = "Otter-AI/MMBench",
        *,
        sys_prompt="There are several options:",
        version="20230712",
        split="test",
        cache_dir=None,
        default_output_path="./logs",
        batch=8,
    ):
        super().__init__("MMBenchDataset", data_path, max_batch_size=batch)
        self.version = str(version)
        self.name_converter = {"dev": "validation", "test": "test"}
        self.df = load_dataset("Otter-AI/MMBench", self.version, split=self.name_converter[split], cache_dir=cache_dir).to_pandas()
        self.default_output_path = default_output_path
        self.sys_prompt = sys_prompt
        self.cur_datetime = utc_plus_8_time.strftime("%Y-%m-%d_%H-%M-%S")

    def load_from_df(self, idx, key):
        if key in self.df.columns:
            value = self.df.loc[idx, key]
            return value if pd.notna(value) else None
        return None

    def create_options_prompt(self, idx, option_candidate):
        available_keys = set(self.df.columns) & set(option_candidate)
        options = {cand: self.load_from_df(idx, cand) for cand in available_keys if self.load_from_df(idx, cand)}
        sorted_options = dict(sorted(options.items()))
        options_prompt = f"{self.sys_prompt}\n"
        for key, item in sorted_options.items():
            options_prompt += f"{key}. {item}\n"
        return options_prompt.rstrip("\n"), sorted_options

    def get_data(self, idx):
        row = self.df.loc[idx]
        option_candidate = ["A", "B", "C", "D", "E"]
        options_prompt, options_dict = self.create_options_prompt(idx, option_candidate)

        data = {
            "img": row["image"],
            "question": row["question"],
            "answer": row.get("answer"),
            "options": options_prompt,
            "category": row["category"],
            "l2-category": row["l2-category"],
            "options_dict": options_dict,
            "index": row["index"],
            "hint": self.load_from_df(idx, "hint"),
            "source": row["source"],
            "split": row["split"],
        }
        return data

    def query_batch(self, model, batch_data):
        batch_data = list(map(self.get_data, batch_data))
        batch_img = [data["img"] for data in batch_data]
        batch_prompt = [f"{data['hint']} {data['question']} {data['options']}" if pd.notna(data["hint"]) else f"{data['question']} {data['options']}" for data in batch_data]
        batch_pred_answer = model.generate(batch_prompt, batch_img)
        return [
            {
                "question": data["question"],
                "answer": data["answer"],
                **data["options_dict"],
                "prediction": pred_answer,
                "hint": data["hint"],
                "source": data["source"],
                "split": data["split"],
                "category": data["category"],
                "l2-category": data["l2-category"],
                "index": data["index"],
            }
            for data, pred_answer in zip(batch_data, batch_pred_answer)
        ]

    def _evaluate(self, model, *, batch=1):
        output_file = os.path.join(self.default_output_path, f"{model.name}_mmbench_eval_result_{self.cur_datetime}.xlsx")
        results = []

        for idx in trange(0, len(self.df), batch):
            results.extend(self.query_batch(model, list(range(idx, min(len(self.df), idx + batch)))))

        df = pd.DataFrame(results)
        with pd.ExcelWriter(output_file, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False)
        print(f"MMBench Evaluator: Result saved to {output_file}.")
