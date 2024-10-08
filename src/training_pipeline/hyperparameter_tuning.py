from comet_ml import Experiment

import joblib

from loguru import logger 
from torch.nn import CrossEntropyLoss
from torch.optim import Adam
from optuna import trial, create_study, Study
from optuna.visualization import plot_param_importances

from src.setup.config import config
from src.setup.paths import TRIALS_DIR

from src.training_pipeline.pretrained_models import get_pretrained_model
from src.training_pipeline.toy_models import BaseCNN, BiggerCNN, DynamicCNN, ResNet, get_resnet


def save_trial_callback(study: Study, frozen_trial: trial.FrozenTrial):
    """
    A callback allowing a given optuna trial to be saved as a pickle file.

    Args:
        study: the optuna optimisation task being run.
        frozen_trial: the trial to be saved.
    """
    trial_name = TRIALS_DIR/f"Trial_{frozen_trial.number}.pkl"
    joblib.dump(value=frozen_trial, filename=trial_name)
 

class BestTrials(trial.Trial):
    """
    This class contains the trials of a given optuna study that optimize the
    average values of the validation loss, recall, accuracy, and precision
    """

    def __init__(self, study: Study):
        super().__init__()
        self.trial_lowest_avg_val_loss = min(study.best_trials, key=lambda t: t.values[0])
        self.trial_highest_avg_val_accuracy = max(study.best_trials, key=lambda t: t.values[1])
        self.trial_highest_val_recall = max(study.best_trials, key=lambda t: t.values[2])
        self.trial_highest_val_precision = max(study.best_trials, key=lambda t: t.values[3])

        self.metrics_and_trials = {
            "avg_val_loss": self.trial_lowest_avg_val_loss,
            "avg_val_accuracy": self.trial_highest_avg_val_accuracy,
            "avg_val_recall": self.trial_highest_val_recall,
            "avg_val_precision": self.trial_highest_val_precision
        }

    def _save_best_trials(self):
        """ Save the best optuna trial for each metric as a pickle file. """

        for item in self.metrics_and_trials.items():
            if "loss" in item[0]:
                joblib.dump(value=item[1], filename=TRIALS_DIR/f"Trial with the lowest {item[0]}.pkl")
            else:
                joblib.dump(value=item[1], filename=TRIALS_DIR/f"Trial with the highest {item[0]}.pkl")

    def _display_best_trials(self):
        """ Display the key details about the best trials """

        for item in self.metrics_and_trials.items():
            if "loss" in item[0]:
                logger.info(f"Trial with lowest {item[0]}:")
            else: 
                logger.info(f"Trial with highest {item[0]}:")

            logger.info(f"number: {item[1].number}")   
            logger.info(f"params: {item[1].params}")
            logger.info(f"values: {item[1].values}")   

    def _view_hyperparam_importances(self, study: Study):
        """ Plots hyperparameter importances """
        metric_names = list(self.metrics_and_trials.keys())
        for name in metric_names:
            plot_param_importances(study=study, target=lambda t: t.values[metric_names.index(name)], target_name=name)

    def _log_with_comet(self):
        """ Log the optimization tasks with CometML """
        experiment = Experiment(api_key=config.comet_api_key, project_name=config.comet_project_name, workspace=config.comet_workspace, log_code=False)   
        for key, value in list(self.metrics_and_trials.items()):
            objective = "minimize" if "loss" in key else "maximize"
            experiment.log_optimization(metric_name=key, metric_value=value, parameters=value.params, objective="minimize")

            
def optimize_hyperparams(
    model_name: str,
    tuning_trials: int,
    batch_size: int,
    experiment: Experiment
    ):

    """
    Using the objective function below, optimise the specified hyperparameters by an 
    optuna study for running the specified number of tuning trials. Then save, display 
    these trials before logging them with CometML 
    """

    def objective(trial: trial.Trial) -> tuple[float, float, float, float]:
        """
        For each optuna trial, initialise values of the hyperparameters within the
        specified ranges, select one of three possible optimizers, and run the 
        training loop to obtain a tuple of metrics on the validation set. 

        Args:
            tuning_trials: the number of hyperparameter tuning trials to be run. 
            experiment: a single instance of a CometML experiment.

        Returns:
            val_metrics: contains a list of floats which are the average values of  the loss,recall, accuracy, 
                         and precision of the trained model on the validation set.
        """
        num_epochs = trial.suggest_int(name="num_epochs", low=5, high=15)
        model_fn = get_tuning_candidate(model_name=model_name, trial=trial)

        criterion = CrossEntropyLoss()
        optimizer_choice = trial.suggest_categorical(name="optimizer", choices=["Adam", "SGD", "RMSProp"])

        if optimizer_choice == "Adam":
            
            # I didn't use get_optimizer() here because I would have to include momentum=None in the arguments because
            # Adam does not have a momentum parameter. Upon doing this, optuna complains about momentum having no value. 
            # It's simply easier to call Adam directly here.
            
            optimizer = Adam(
                params=model_fn.parameters(),
                lr=trial.suggest_float(name="lr", low=1e-5, high=1e-1, log=True),
                weight_decay=trial.suggest_float(name="weight_decay", low = 0.001, high = 0.08, log=True),
            )

        from src.training_pipeline.training import get_optimizer, run_training_loop
        optimizer = get_optimizer(
            model_fn=model_fn,
            optimizer_name=optimizer_choice, 
            learning_rate=trial.suggest_float(name="lr", low=1e-5, high=1e-1, log=True),
            weight_decay=trial.suggest_float(name="weight_decay", low = 0.001, high = 0.08, log=True),
            momentum=trial.suggest_float(name="momentum", low=0.1, high=0.9)
        )
        
        val_metrics = run_training_loop(
            model_fn=model_fn, 
            criterion=criterion,
            optimizer=optimizer,
            num_epochs=num_epochs,
            batch_size=batch_size,
            save=True
        )
        
        return val_metrics
            
    logger.info("Searching for optimal values of the hyperparameters")
    study = create_study(directions=["minimize", "maximize", "maximize", "maximize"])  # Study that corresponds to the metrics we want to optimize
    study.optimize(func=objective, n_trials=tuning_trials, callbacks=[save_trial_callback], timeout=300)

    logger.info(f"Number of finished trials: {len(study.trials)}")
    best_trials = BestTrials(study=study)
    best_trials._save_best_trials()
    best_trials._display_best_trials()
    best_trials._log_with_comet()


def get_tuning_candidate(model_name: str, trial: trial.Trial) -> callable:

    if model_name.lower() == "base":
        return BaseCNN()

    elif model_name.lower() == "dynamic":
        conv_layers = trial.suggest_int(name="num_conv_layers", low=2, high=6)
        out_channels = trial.suggest_int(name="out_channels", low=16, high=96)
        dropout = trial.suggest_int(name="dropout", low=0.05, high=0.5)
        prev_out_channels = 16

        layer_configs = []       
        for _ in range(conv_layers):
            stride = trial.suggest_int(name="stride", low=1, high=3)
            padding = "same" if stride == 1 else "valid"  # "Same" padding is not supported for strided convolutions

            conv_config = {
                "type": "conv", "out_channels": prev_out_channels, "kernel_size": 3, "stride": stride, "pooling": True,
                "padding": padding
            }

            layer_configs.append(conv_config)
            prev_out_channels*=2  # Arbitrary choice

        return DynamicCNN(in_channels=3, num_classes=num_classes, layer_configs=layer_configs, dropout=dropout)

    elif model_name.lower() == "bigger":
        return BiggerCNN(in_channels=3, num_classes=num_classes, tune_hyperparams=True, trial=trial)

    elif model_name.lower() in ["resnet50", "resnet101", "resnet152"]:
        return get_resnet(model_name=model_name.lower())

    elif model_name in ["vit", "hybrid_vit", "beit"]: 
        return get_pretrained_model(model_name=model_name)
