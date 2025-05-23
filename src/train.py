import os
from copy import deepcopy
from datetime import datetime

import torch
import yaml
from ultralytics import YOLO
from ultralytics.yolo.utils import __version__
from ultralytics.yolo.utils.torch_utils import de_parallel

from src.logger import get_logger
from src.utils import check_dataset_structure, unzip_file

logger = get_logger("ultralytics_gdrive_ops")


class Trainer:
    def __init__(self, run_name: str, data_path: str, model_log_path: str):
        self.run_name = run_name
        self.data_path = data_path
        self.model_log_path = model_log_path

    @staticmethod
    def prepare_dataset(dataset_path: str) -> str | None:
        """
        Prepare the dataset for training.
        """
        if not dataset_path.endswith('.zip'):
            logger.error(f"Dataset path must end with .zip: {dataset_path}")
            return None

        save_dir = os.path.dirname(dataset_path)
        unzip_file(dataset_path, save_dir)

        dataset_path = os.path.splitext(dataset_path)[0]

        if not check_dataset_structure(dataset_path):
            return None

        return dataset_path

    def train(self):
        """
        Train the model.
        """
        model = YOLO("yolov8x.pt")

        config_file = os.path.join(self.data_path, "data.yaml")
        if not os.path.exists(config_file):
            data_yaml = {
                "path": os.path.join(os.getcwd(), self.data_path),
                "train": "train/images",
                "val": "val/images",
                "names": {
                    0: "defect",
                }
            }
        else:
            with open(config_file, "r") as f:
                data_yaml = yaml.safe_load(f)
            data_yaml["path"] = os.path.join(os.getcwd(), self.data_path)

        with open(config_file, "w") as f:
            yaml.dump(data_yaml, f)

        model.add_callback("on_fit_epoch_end", save_top10_models_callback)
        model.train(
            data=config_file,
            epochs=400,
            imgsz=1280,
            batch=8,
            name=self.run_name,
            project=os.path.join(os.getcwd(), self.model_log_path),
            exist_ok=True,
            save_period=-1,
            fliplr=0.8,
            flipud=0.6,
            scale=0.1,
            patience=200
        )

        model.export(format="onnx", imgsz=1280)


import heapq


def save_top10_models_callback(trainer):
    """
    Callback to save top 10 models based on fitness score.
    """
    # Initialize top10 heap on first call
    if not hasattr(trainer, "_top10_models"):
        trainer._top10_models = []  # min-heap of (fitness, epoch, path)
        trainer._top10_ckpt_paths = set()

    # Prepare checkpoint
    ckpt = {
        'epoch': trainer.epoch,
        'best_fitness': trainer.best_fitness,
        'model': deepcopy(de_parallel(trainer.model)).half(),
        'ema': deepcopy(trainer.ema.ema).half(),
        'updates': trainer.ema.updates,
        'optimizer': trainer.optimizer.state_dict(),
        'train_args': vars(trainer.args),  # save as dict
        'date': datetime.now().isoformat(),
        'version': __version__
    }

    map_score = trainer.metrics["metrics/mAP50-95(B)"]
    epoch = trainer.epoch
    top10 = trainer._top10_models
    ckpt_name = f"top10_epoch{epoch}_map5095_{map_score:.6f}.pt"
    ckpt_path = trainer.wdir / ckpt_name

    # If less than 10, just add
    if len(top10) < 10:
        heapq.heappush(top10, (map_score, epoch, str(ckpt_path)))
        trainer._top10_ckpt_paths.add(str(ckpt_path))
        torch.save(ckpt, ckpt_path)
    else:
        # If current score is better than the worst in top10, replace
        if map_score > top10[0][0]:
            # Remove the worst
            _, _, worst_path = heapq.heappop(top10)
            if worst_path in trainer._top10_ckpt_paths:
                try:
                    os.remove(worst_path)
                except Exception:
                    pass
                trainer._top10_ckpt_paths.remove(worst_path)
            # Add the new one
            heapq.heappush(top10, (map_score, epoch, str(ckpt_path)))
            trainer._top10_ckpt_paths.add(str(ckpt_path))
            torch.save(ckpt, ckpt_path)

    del ckpt


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--run_name", type=str, required=True)
    parser.add_argument("--data_path", type=str, required=True)
    parser.add_argument("--model_log_path", type=str, required=True)
    args = parser.parse_args()

    trainer = Trainer(args.run_name, args.data_path, args.model_log_path)
    trainer.train()