import base64
import sys
from dataclasses import dataclass
from io import BytesIO
from os import path
from typing import Dict, Optional

import numpy as np
import torch
import torchvision
import torchvision.models as models
import torchvision.transforms as T
from PIL import Image as PILImage

from lightning import seed_everything
from pytorch_lightning import cli_lightning_logo, LightningDataModule, LightningModule
from pytorch_lightning.serve import ServableModule, ServableModuleValidator
from pytorch_lightning.utilities.cli import LightningCLI

DEFAULT_CMD_LINE = (
    "fit",
    "--trainer.max_epochs=1",
    "--trainer.limit_train_batches=15",
    "--trainer.limit_val_batches=15",
)
DATASETS_PATH = path.join(path.dirname(__file__), "..", "..", "Datasets")


class LitModule(LightningModule):
    def __init__(self, name: str = "resnet18"):
        super().__init__()
        self.model = getattr(models, name)(pretrained=True)
        self.model.fc = torch.nn.Linear(self.model.fc.in_features, 10)
        self.criterion = torch.nn.CrossEntropyLoss()

    def training_step(self, batch, batch_idx):
        inputs, labels = batch
        outputs = self.model(inputs)
        loss = self.criterion(outputs, labels)
        self.log("train_loss", loss)
        return loss

    def validation_step(self, batch, batch_idx):
        inputs, labels = batch
        outputs = self.model(inputs)
        loss = self.criterion(outputs, labels)
        self.log("val_loss", loss)

    def configure_optimizers(self):
        return torch.optim.SGD(self.parameters(), lr=0.001, momentum=0.9)


class CIFAR10DataModule(LightningDataModule):

    transform = T.Compose([T.Resize(256), T.CenterCrop(224), T.ToTensor()])

    def train_dataloader(self, *args, **kwargs):
        trainset = torchvision.datasets.CIFAR10(root=DATASETS_PATH, train=True, download=True, transform=self.transform)
        return torch.utils.data.DataLoader(trainset, batch_size=2, shuffle=True, num_workers=0)

    def val_dataloader(self, *args, **kwargs):
        valset = torchvision.datasets.CIFAR10(root=DATASETS_PATH, train=False, download=True, transform=self.transform)
        return torch.utils.data.DataLoader(valset, batch_size=2, shuffle=True, num_workers=0)


@dataclass(unsafe_hash=True)
class Image:

    height: Optional[int] = None
    width: Optional[int] = None
    extension: str = "JPEG"
    mode: str = "RGB"
    channel_first: bool = False

    def deserialize(self, data: str) -> torch.Tensor:
        encoded_with_padding = (data + "===").encode("ascii")
        img = base64.b64decode(encoded_with_padding)
        buffer = BytesIO(img)
        img = PILImage.open(buffer, mode="r")
        if self.height and self.width:
            img = img.resize((self.width, self.height))
        arr = np.array(img)
        return T.ToTensor()(arr).unsqueeze(0)


class Top1:
    def serialize(self, tensor: torch.Tensor) -> int:
        return torch.nn.functional.softmax(tensor).argmax().item()


class ProductionReadyModel(LitModule, ServableModule):
    def configure_payload(self):
        # 1: Access the train dataloader and load a single sample.
        image, _ = self.trainer.train_dataloader.loaders.dataset[0]

        # 2: Convert the image into a PIL Image and save it to disk
        pil_image = T.ToPILImage()(image)
        pil_image.save("payload_image.png")

        # 3: Convert the image to bytes and encode it with base64
        buffered = BytesIO()
        pil_image.save(buffered, format="JPEG")
        img_str = base64.b64encode(buffered.getvalue()).decode("UTF-8")

        payload = {"body": {"x": img_str}}
        return payload

    def configure_serialization(self):
        return {"x": Image(224, 224).deserialize}, {"output": Top1().serialize}

    def serve_step(self, x: torch.Tensor) -> Dict[str, torch.Tensor]:
        return {"output": self.model(x)}

    def configure_response(self):
        return {"output": 7}


def cli_main():

    seed_everything(42)

    if len(sys.argv) == 1:
        sys.argv += DEFAULT_CMD_LINE

    LightningCLI(
        ProductionReadyModel,
        CIFAR10DataModule,
        save_config_overwrite=True,
        trainer_defaults={"callbacks": [ServableModuleValidator()]},
    )


if __name__ == "__main__":
    cli_lightning_logo()
    cli_main()