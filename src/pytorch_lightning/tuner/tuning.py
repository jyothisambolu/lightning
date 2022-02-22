# Copyright The PyTorch Lightning team.
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
from typing import Any, Dict, Optional, Union

import pytorch_lightning as pl
from pytorch_lightning.callbacks.batch_size_finder import BatchSizeFinder
from pytorch_lightning.core.datamodule import LightningDataModule
from pytorch_lightning.trainer.states import TrainerFn, TrainerStatus
from pytorch_lightning.tuner.lr_finder import _LRFinder, lr_find
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.types import EVAL_DATALOADERS, TRAIN_DATALOADERS


class Tuner:
    """Tuner class to tune your model."""

    def __init__(self, trainer: "pl.Trainer") -> None:
        self.trainer = trainer

    def on_trainer_init(self, auto_lr_find: Union[str, bool], auto_scale_batch_size: Union[str, bool]) -> None:
        self.trainer.auto_lr_find = auto_lr_find
        self.trainer.auto_scale_batch_size = auto_scale_batch_size

    def _tune(
        self,
        model: "pl.LightningModule",
        train_dataloaders: Optional[Union[TRAIN_DATALOADERS, LightningDataModule]] = None,
        val_dataloaders: Optional[EVAL_DATALOADERS] = None,
        dataloaders: Optional[EVAL_DATALOADERS] = None,
        datamodule: Optional[LightningDataModule] = None,
        scale_batch_size_kwargs: Optional[Dict[str, Any]] = None,
        lr_find_kwargs: Optional[Dict[str, Any]] = None,
        method: str = "fit",
    ) -> Dict[str, Optional[Union[int, _LRFinder]]]:
        scale_batch_size_kwargs = scale_batch_size_kwargs or {}
        lr_find_kwargs = lr_find_kwargs or {}
        # return a dict instead of a tuple so BC is not broken if a new tuning procedure is added
        result = {}

        self.trainer.strategy.connect(model)

        is_tuning = self.trainer.auto_scale_batch_size or self.trainer.auto_lr_find
        if self.trainer._accelerator_connector.is_distributed and is_tuning:
            raise MisconfigurationException(
                "`trainer.tune()` is currently not supported with"
                f" `Trainer(strategy={self.trainer.strategy.strategy_name!r})`."
            )

        # Run auto batch size scaling
        if self.trainer.auto_scale_batch_size:
            if isinstance(self.trainer.auto_scale_batch_size, str):
                scale_batch_size_kwargs.setdefault("mode", self.trainer.auto_scale_batch_size)

            result["scale_batch_size"] = self.scale_batch_size(
                model, train_dataloaders, val_dataloaders, dataloaders, datamodule, method, **scale_batch_size_kwargs
            )

        # Run learning rate finder:
        if self.trainer.auto_lr_find:
            self.trainer.state.fn = TrainerFn.TUNING
            self.trainer.state.status = TrainerStatus.RUNNING
            self.tuning = True

            # TODO: Remove this once LRFinder is converted to a Callback
            # if a datamodule comes in as the second arg, then fix it for the user
            if isinstance(train_dataloaders, LightningDataModule):
                datamodule = train_dataloaders
                train_dataloaders = None

            # If you supply a datamodule you can't supply train_dataloader or val_dataloaders
            if (train_dataloaders is not None or val_dataloaders is not None) and datamodule is not None:
                raise MisconfigurationException(
                    "You cannot pass `train_dataloader` or `val_dataloaders` to `trainer.tune()`"
                    " if datamodule is already passed."
                )

            # links da_a to the trainer
            self.trainer._data_connector.attach_data(
                model, train_dataloaders=train_dataloaders, val_dataloaders=val_dataloaders, datamodule=datamodule
            )

            lr_find_kwargs.setdefault("update_attr", True)
            result["lr_find"] = lr_find(self.trainer, model, **lr_find_kwargs)
            self.trainer.state.status = TrainerStatus.FINISHED

        return result

    def _run(self, *args: Any, **kwargs: Any) -> None:
        """`_run` wrapper to set the proper state during tuning, as this can be called multiple times."""
        self.trainer.state.status = TrainerStatus.RUNNING  # last `_run` call might have set it to `FINISHED`
        self.trainer.training = True
        self.trainer._run(*args, **kwargs)
        self.trainer.tuning = True

    def scale_batch_size(
        self,
        model: "pl.LightningModule",
        train_dataloaders: Optional[Union[TRAIN_DATALOADERS, "pl.LightningDataModule"]] = None,
        val_dataloaders: Optional[EVAL_DATALOADERS] = None,
        dataloaders: Optional[EVAL_DATALOADERS] = None,
        datamodule: Optional["pl.LightningDataModule"] = None,
        method: str = "fit",
        mode: str = "power",
        steps_per_trial: int = 3,
        init_val: int = 2,
        max_trials: int = 25,
        batch_arg_name: str = "batch_size",
    ) -> Optional[int]:
        """Iteratively try to find the largest batch size for a given model that does not give an out of memory
        (OOM) error.

        Args:
            model: Model to tune.

            train_dataloaders: A collection of :class:`torch.utils.data.DataLoader` or a
                :class:`~pytorch_lightning.core.datamodule.LightningDataModule` specifying training samples.
                In the case of multiple dataloaders, please see this :ref:`section <multiple-dataloaders>`.

            val_dataloaders: A :class:`torch.utils.data.DataLoader` or a sequence of them specifying validation samples.

            dataloaders: A :class:`torch.utils.data.DataLoader` or a sequence of them specifying val/test/predict
                samples used for running tuner on validation/testing/prediction.

            datamodule: An instance of :class:`~pytorch_lightning.core.datamodule.LightningDataModule`.

            method: Method to run tuner on. It can be ``'fit', 'validate', 'test', 'predict'``

            mode: Search strategy to update the batch size:

                - ``'power'``: Keep multiplying the batch size by 2, until we get an OOM error.
                - ``'binsearch'``: Initially keep multiplying by 2 and after encountering an OOM error
                    do a binary search between the last successful batch size and the batch size that failed.

            steps_per_trial: number of steps to run with a given batch size.
                Ideally 1 should be enough to test if a OOM error occurs,
                however in practise a few are needed

            init_val: initial batch size to start the search with

            max_trials: max number of increase in batch size done before
               algorithm is terminated

            batch_arg_name: name of the attribute that stores the batch size.
                It is expected that the user has provided a model or datamodule that has a hyperparameter
                with that name. We will look for this attribute name in the following places

                - ``model``
                - ``model.hparams``
                - ``trainer.datamodule`` (the datamodule passed to the tune method)
        """
        self.trainer.state.fn = TrainerFn.TUNING
        self.tuning = True

        _check_tuner_configuration(self.trainer, train_dataloaders, val_dataloaders, dataloaders, method)

        batch_size_finder = BatchSizeFinder(
            mode=mode,
            steps_per_trial=steps_per_trial,
            init_val=init_val,
            max_trials=max_trials,
            batch_arg_name=batch_arg_name,
        )
        # do not continue with the loop in case trainer.tuner is used
        batch_size_finder._early_exit = True
        self.trainer.callbacks = [batch_size_finder] + self.trainer.callbacks

        if method == "fit":
            self.trainer.fit(model, train_dataloaders, val_dataloaders, datamodule)
        elif method == "validate":
            self.trainer.validate(model, dataloaders, datamodule)
        elif method == "test":
            self.trainer.test(model, dataloaders, datamodule)
        elif method == "predict":
            self.trainer.predict(model, dataloaders, datamodule)

        self.trainer.callbacks = [cb for cb in self.trainer.callbacks if cb is not batch_size_finder]
        self.trainer.auto_scale_batch_size = False
        return batch_size_finder.optimal_batch_size

    def lr_find(
        self,
        model: "pl.LightningModule",
        train_dataloaders: Optional[Union[TRAIN_DATALOADERS, "pl.LightningDataModule"]] = None,
        val_dataloaders: Optional[EVAL_DATALOADERS] = None,
        datamodule: Optional["pl.LightningDataModule"] = None,
        min_lr: float = 1e-8,
        max_lr: float = 1,
        num_training: int = 100,
        mode: str = "exponential",
        early_stop_threshold: float = 4.0,
        update_attr: bool = False,
    ) -> Optional[_LRFinder]:
        """Enables the user to do a range test of good initial learning rates, to reduce the amount of guesswork in
        picking a good starting learning rate.

        Args:
            model: Model to tune.

            train_dataloaders: A collection of :class:`torch.utils.data.DataLoader` or a
                :class:`~pytorch_lightning.core.datamodule.LightningDataModule` specifying training samples.
                In the case of multiple dataloaders, please see this :ref:`section <multiple-dataloaders>`.

            val_dataloaders: A :class:`torch.utils.data.DataLoader` or a sequence of them specifying validation samples.

            datamodule: An instance of :class:`~pytorch_lightning.core.datamodule.LightningDataModule`.

            min_lr: minimum learning rate to investigate

            max_lr: maximum learning rate to investigate

            num_training: number of learning rates to test

            mode: Search strategy to update learning rate after each batch:

                - ``'exponential'`` (default): Will increase the learning rate exponentially.
                - ``'linear'``: Will increase the learning rate linearly.

            early_stop_threshold: threshold for stopping the search. If the
                loss at any point is larger than early_stop_threshold*best_loss
                then the search is stopped. To disable, set to None.

            update_attr: Whether to update the learning rate attribute or not.

        Raises:
            MisconfigurationException:
                If learning rate/lr in ``model`` or ``model.hparams`` isn't overridden when ``auto_lr_find=True``,
                or if you are using more than one optimizer.
        """
        self.trainer.auto_lr_find = True
        result = self.trainer.tune(
            model,
            train_dataloaders=train_dataloaders,
            val_dataloaders=val_dataloaders,
            datamodule=datamodule,
            lr_find_kwargs={
                "min_lr": min_lr,
                "max_lr": max_lr,
                "num_training": num_training,
                "mode": mode,
                "early_stop_threshold": early_stop_threshold,
                "update_attr": update_attr,
            },
        )
        self.trainer.auto_lr_find = False
        return result["lr_find"]


def _check_tuner_configuration(
    trainer: "pl.Trainer",
    train_dataloaders: Optional[Union[TRAIN_DATALOADERS, "pl.LightningDataModule"]] = None,
    val_dataloaders: Optional[EVAL_DATALOADERS] = None,
    dataloaders: Optional[EVAL_DATALOADERS] = None,
    method: str = "fit",
):
    supported_methods = ("fit", "validate", "test", "predict")
    if method not in supported_methods:
        raise MisconfigurationException(f"method {method!r} is invalid. Should be one of {supported_methods}.")

    if method == "fit":
        if dataloaders is not None:
            raise MisconfigurationException(
                f"In tuner with method={method!r}, `dataloaders` argument should be None,"
                " please consider setting `train_dataloaders` and `val_dataloaders` instead."
            )
    else:
        if train_dataloaders is not None or val_dataloaders is not None:
            raise MisconfigurationException(
                f"In tuner with `method`={method!r}, `train_dataloaders` and `val_dataloaders`"
                " arguments should be None, please consider setting `dataloaders` instead."
            )

    if any(isinstance(cb, BatchSizeFinder) for cb in trainer.callbacks):
        raise MisconfigurationException(
            "Trainer is already configured with a `BatchSizeFinder` callback. Please remove it if you"
            " want to use tuner."
        )
