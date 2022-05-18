import copy
import gc
import math
from collections import deque
import logging
from time import perf_counter
from typing import Optional, TextIO, List, NoReturn, Tuple, Union, Dict, Set

import numpy as np
import torch
from numpy.random import RandomState
from sklearn.metrics import mean_squared_error

# from .Ridge import Ridge
from sklearn.linear_model import Ridge
from .BayesianRegression import BayesianRidge, ARDRegression
from ..structure.AndGate import AndGate, AndChildNode
from ..structure.CircuitNode import OrGate, CircuitTerminal
from ..structure.CircuitNode import LITERAL_IS_TRUE, LITERAL_IS_FALSE
from ..structure.Vtree import Vtree
from ..util.DataSet import DataSet

FORMAT = """c variables (from inputs) start from 1
c ids of logistic circuit nodes start from 0
c nodes appear bottom-up, children before parents
c the last line of the file records the bias parameter
c three types of nodes:
c	T (terminal nodes that correspond to true literals)
c	F (terminal nodes that correspond to false literals)
c	D (OR gates)
c
c file syntax:
c Regression Circuit
c T id-of-true-literal-node id-of-vtree variable parameters
c F id-of-false-literal-node id-of-vtree variable parameters
c D id-of-or-gate id-of-vtree number-of-elements (id-of-prime id-of-sub parameters)s
c B bias-parameters
c V covariances
c
"""


# TODO: I feel this should extend LogisticCircuit instead of copying it, or maybe a shared base class
class RegressionCircuit(object):
    _vtree: Vtree
    _num_classes: int
    _largest_index: int
    _num_variables: int

    rand_gen: RandomState

    _terminal_nodes: List[Optional[CircuitTerminal]]
    _decision_nodes: Optional[List[OrGate]]
    _elements: Optional[List[AndGate]]
    _parameters: Optional[torch.Tensor]
    _covariance: Optional[List[np.ndarray]]
    _bias: torch.Tensor
    _root: OrGate

    def __init__(self, vtree: Vtree, circuit_file: Optional[TextIO] = None,
                 rand_gen: Optional[RandomState] = None):

        self._vtree = vtree
        self._num_classes = 1
        self._largest_index = 0
        self._num_variables = vtree.var_count

        if rand_gen is None:
            rand_gen = np.random.RandomState(1337)
        self.rand_gen = rand_gen

        self._terminal_nodes = [None] * 2 * self._num_variables
        self._decision_nodes = None
        self._elements = None
        self._parameters = None
        self._covariance = None
        self._bias = torch.tensor(self.rand_gen.random_sample(size=1))

        if circuit_file is None:
            self._generate_all_terminal_nodes(vtree)
            self._root = self._new_regression_psdd(vtree)
        else:
            self._root = self.load(circuit_file)

        self._serialize()

    @property
    def vtree(self) -> Vtree:
        return self._vtree

    @property
    def num_variables(self) -> int:
        return self._num_variables

    @property
    def root(self) -> OrGate:
        return self._root

    @property
    def num_parameters(self) -> int:
        # numpy return self._parameters.size
        return self._parameters.size()

    @property
    def parameters(self) -> Optional[torch.Tensor]:
        return self._parameters

    @property
    def bias(self) -> torch.Tensor:
        return self._bias

    def _generate_all_terminal_nodes(self, vtree: Vtree) -> NoReturn:
        if vtree.is_leaf():
            var_index = vtree.var
            self._terminal_nodes[var_index - 1] = CircuitTerminal(
                self._largest_index, vtree, var_index, LITERAL_IS_TRUE,
                torch.tensor(self.rand_gen.random_sample(size=1))
            )
            self._largest_index += 1
            self._terminal_nodes[self._num_variables + var_index - 1] = CircuitTerminal(
                self._largest_index, vtree, var_index, LITERAL_IS_FALSE,
                torch.tensor(self.rand_gen.random_sample(size=1))
            )
            self._largest_index += 1
        else:
            self._generate_all_terminal_nodes(vtree.left)
            self._generate_all_terminal_nodes(vtree.right)

    # Same as LogisticCircuit except size=(self._num_classes,) becomes size=1
    def _new_regression_psdd(self, vtree: Vtree) -> OrGate:
        left_vtree: Vtree = vtree.left
        right_vtree: Vtree = vtree.right
        prime_variable: int = left_vtree.var
        sub_variable: int = right_vtree.var
        elements: List[AndGate] = list()
        if left_vtree.is_leaf() and right_vtree.is_leaf():
            elements.append(
                AndGate(
                    self._terminal_nodes[prime_variable - 1],
                    self._terminal_nodes[sub_variable - 1],
                    torch.tensor(self.rand_gen.random_sample(size=1))
                )
            )
            elements.append(
                AndGate(
                    self._terminal_nodes[prime_variable - 1],
                    self._terminal_nodes[self._num_variables + sub_variable - 1],
                    torch.tensor(self.rand_gen.random_sample(size=1))
                )
            )
            elements.append(
                AndGate(
                    self._terminal_nodes[self._num_variables + prime_variable - 1],
                    self._terminal_nodes[sub_variable - 1],
                    torch.tensor(self.rand_gen.random_sample(size=1))
                )
            )
            elements.append(
                AndGate(
                    self._terminal_nodes[self._num_variables + prime_variable - 1],
                    self._terminal_nodes[self._num_variables + sub_variable - 1],
                    torch.tensor(self.rand_gen.random_sample(size=1))
                )
            )
        elif left_vtree.is_leaf():
            elements.append(
                AndGate(
                    self._terminal_nodes[prime_variable - 1],
                    self._new_regression_psdd(right_vtree),
                    torch.tensor(self.rand_gen.random_sample(size=1))
                )
            )
            elements.append(
                AndGate(
                    self._terminal_nodes[self._num_variables + prime_variable - 1],
                    self._new_regression_psdd(right_vtree),
                    torch.tensor(self.rand_gen.random_sample(size=1))
                )
            )
            for element in elements:
                element.splittable_variables = copy.deepcopy(right_vtree.variables)
        elif right_vtree.is_leaf():
            elements.append(
                AndGate(
                    self._new_regression_psdd(left_vtree),
                    self._terminal_nodes[sub_variable - 1],
                    torch.tensor(self.rand_gen.random_sample(size=1))
                )
            )
            elements.append(
                AndGate(
                    self._new_regression_psdd(left_vtree),
                    self._terminal_nodes[self._num_variables + sub_variable - 1],
                    torch.tensor(self.rand_gen.random_sample(size=1))
                )
            )
            for element in elements:
                element.splittable_variables = copy.deepcopy(left_vtree.variables)
        else:
            elements.append(
                AndGate(
                    self._new_regression_psdd(left_vtree),
                    self._new_regression_psdd(right_vtree),
                    torch.tensor(self.rand_gen.random_sample(size=1))
                )
            )
            elements[0].splittable_variables = copy.deepcopy(vtree.variables)
        root = OrGate(self._largest_index, vtree, elements)
        self._largest_index += 1
        return root

    # Same as logistic circuit
    def _serialize(self) -> NoReturn:
        """Serialize all the decision nodes in the logistic psdd.
           Serialize all the elements in the logistic psdd. """
        self._decision_nodes = [self._root]
        self._elements = []
        decision_node_indices = set()
        decision_node_indices.add(self._root.index)
        unvisited: deque[OrGate] = deque()
        unvisited.append(self._root)
        while len(unvisited) > 0:
            current = unvisited.popleft()
            for element in current.elements:
                self._elements.append(element)
                element.flag = False
                if isinstance(element.prime, OrGate) and element.prime.index not in decision_node_indices:
                    decision_node_indices.add(element.prime.index)
                    self._decision_nodes.append(element.prime)
                    unvisited.append(element.prime)
                if isinstance(element.sub, OrGate) and element.sub.index not in decision_node_indices:
                    decision_node_indices.add(element.sub.index)
                    self._decision_nodes.append(element.sub)
                    unvisited.append(element.sub)
        self._parameters = self._bias.reshape(-1, 1)
        for terminal_node in self._terminal_nodes:
            # print(self._parameters.shape, terminal_node.parameter.reshape(-1, 1).shape)
            self._parameters = torch.cat((self._parameters, terminal_node.parameter.reshape(-1, 1)), dim=1)
        for element in self._elements:
            self._parameters = torch.cat((self._parameters, element.parameter.reshape(-1, 1)), dim=1)
        gc.collect()

    # Same as LogisticCircuit except the parameter copying
    def _record_learned_parameters(self, parameters: np.ndarray, requires_grad: bool = False) -> NoReturn:
        # TODO: reshape required?
        self._parameters = torch.tensor(parameters, requires_grad=requires_grad).reshape(1, -1)
        # print("todo fix the _record_learned_parameters")

        print('PARAMS', self._parameters.shape)
        self._bias = self._parameters[:, 0]

        for i in range(len(self._terminal_nodes)):
            self._terminal_nodes[i].parameter = self._parameters[:, i + 1]
        for i in range(len(self._elements)):
            self._elements[i].parameter = self._parameters[:, i + 1 + 2 * self._num_variables]
        gc.collect()

    # Identical to LogisticCircuit
    def calculate_features(self, images: np.ndarray) -> np.ndarray:
        num_images: int = images.shape[0]
        for terminal_node in self._terminal_nodes:
            terminal_node.calculate_prob(images)
        for decision_node in reversed(self._decision_nodes):
            decision_node.calculate_prob()
        self._root.feature = np.ones(shape=(num_images,), dtype=np.float64)
        for decision_node in self._decision_nodes:
            decision_node.calculate_feature()
        # bias feature
        bias_features = np.ones(shape=(num_images,), dtype=np.float64)
        terminal_node_features = np.vstack(
            [terminal_node.feature for terminal_node in self._terminal_nodes])
        element_features = np.vstack([element.feature for element in self._elements])
        features = np.vstack((bias_features, terminal_node_features, element_features))
        for terminal_node in self._terminal_nodes:
            terminal_node.feature = None
            terminal_node.prob = None
        for element in self._elements:
            element.feature = None
            element.prob = None
        return features.T

    # Two additional parameters over LogisticCircuit
    def _select_element_and_variable_to_split(self, data: DataSet, num_splits: int,
                                              alpha: float, min_candidate_list: int = 5000) -> List[Tuple[AndGate, int]]:
        # y = self.predict_prob(data.features)
        y: torch.Tensor = self.predict(data.features)

        delta: np.ndarray = (data.labels.reshape(-1, 1) - y).numpy()
        # element_gradients = - 2 * np.dot(data.features.T,  delta) + (2 * alpha * self.parameters).T
        # TODO: we are using torch elsewhere for gradients, feel like here might as well use it later
        element_gradients = (-2 * (delta.reshape(-1, 1) * data.features + (2 * alpha * self.parameters.numpy()))[:, 2 * self._num_variables + 1:])
        # logging.info(
        #     f'e g shape {len(element_gradients)} delta {delta.shape} {data.features.shape}')
        element_gradient_variance = np.var(element_gradients, axis=0)

        # logging.info(
        #     f'\tgradient variances: shape:{element_gradient_variance.shape} max: {np.max(element_gradient_variance)} min: {np.min(element_gradient_variance)}')
        # delta = data.one_hot_labels - y
        # element_gradients = np.stack(
        #     [(delta[:, i].reshape(-1, 1) * data.features)[:, 2 * self._num_variables + 1:]
        #      for i in range(self._num_classes)],
        #     axis=0)
        # element_gradient_variance = np.var(element_gradients, axis=1)
        # element_gradient_variance = np.average(element_gradient_variance, axis=0)

        # print(element_gradient_variance, 'EGV', element_gradient_variance.shape)

        candidates: List[Tuple[AndGate, np.ndarray, np.ndarray]] = sorted(
            zip(self._elements, element_gradient_variance,
                data.features.T[2 * self._num_variables + 1:]),
            reverse=True,
            key=lambda x: x[1],
        )
        selected: List[Tuple[Tuple[AndGate, int], float]] = []
        logging.info(f"{len(list(candidates))} candidates found")
        # print(len(candidates), candidates)
        for (element_to_split, original_variance, original_feature) in candidates[: min(min_candidate_list, len(candidates))]:
            if len(element_to_split.splittable_variables) > 0 and np.sum(original_feature) > 25:
                # logging.info(
                #     f'vars to split {len(element_to_split.splittable_variables)} {np.sum(candidate[2]) > 25}')
                variable_to_split: Optional[int] = None
                min_after_split_variance = float("inf")
                # print('SPLIT', element_to_split.splittable_variables)
                for variable in element_to_split.splittable_variables:
                    variable: int
                    left_feature = original_feature * data.images[:, variable - 1]
                    right_feature = original_feature - left_feature

                    # print('FEATs', left_feature.shape, right_feature.shape)
                    # print('SUM left', np.sum(left_feature), np.sum(right_feature))
                    #
                    #
                    # FIXME: this is hardcoded, disabling it for a moment
                    # if np.sum(left_feature) > 10 and np.sum(right_feature) > 10:

                    # left_gradient = (data.one_hot_labels - y) * left_feature.reshape((-1, 1))
                    # right_gradient = (data.one_hot_labels - y) * right_feature.reshape((-1, 1))
                    left_gradient = -2 * np.dot(left_feature.reshape((-1, 1)).T,
                                                delta) + (2 * alpha * self.parameters).T
                    right_gradient = -2 * \
                        np.dot(right_feature.reshape((-1, 1)).T, delta) + \
                        (2 * alpha * self.parameters).T

                    # logging.info(f'LG {left_gradient.shape} RG {right_gradient.shape}')
                    w = np.sum(data.images[:, variable - 1]) / data.num_samples

                    after_split_variance = w * np.average(np.var(left_gradient, axis=0)) + (1 - w) * np.average(
                        np.var(right_gradient, axis=0)
                    )
                    if after_split_variance < min_after_split_variance:
                        min_after_split_variance = after_split_variance
                        variable_to_split = variable
                # print('VARS', min_after_split_variance, 'ORIG', original_variance)
                if min_after_split_variance < original_variance:
                    assert variable_to_split is not None
                    improved_amount = min_after_split_variance - original_variance
                    # print('imp amount', improved_amount, len(selected))
                    if len(selected) == num_splits:
                        if improved_amount < selected[0][1]:
                            selected = selected[1:]
                            selected.append(((element_to_split, variable_to_split), improved_amount))
                            selected.sort(key=lambda x: x[1])
                    else:
                        selected.append(((element_to_split, variable_to_split), improved_amount))
                        selected.sort(key=lambda x: x[1])

        gc.collect()
        splits = [x[0] for x in selected]
        logging.info(f"{len(splits)} splits found")
        return splits

    # Identical to LogisticCircuit
    def _split(self, element_to_split: AndGate, variable_to_split: int, depth: int) -> NoReturn:
        parent = element_to_split.parent
        original_element, copied_element = self._copy_and_modify_element_for_split(
            element_to_split, variable_to_split, 0, depth
        )
        if original_element is None or copied_element is None:
            raise ValueError("Split elements become invalid.")
        parent.add_element(copied_element)

    # Identical to LogisticCircuit
    def _copy_and_modify_element_for_split(self, original_element: AndGate, variable: int, current_depth: int,
                                           max_depth: int) -> Tuple[Optional[AndGate], Optional[AndGate]]:
        original_element.flag = True
        original_element.remove_splittable_variable(variable)
        original_prime = original_element.prime
        original_sub = original_element.sub
        copied_element: Optional[AndGate]
        if current_depth >= max_depth:
            if variable in original_prime.vtree.variables:
                original_prime, copied_prime = self._copy_and_modify_node_for_split(
                    original_prime, variable, current_depth, max_depth
                )
                copied_sub = original_sub
            elif variable in original_sub.vtree.variables:
                original_sub, copied_sub = self._copy_and_modify_node_for_split(
                    original_sub, variable, current_depth, max_depth
                )
                copied_prime = original_prime
            else:
                copied_prime = original_prime
                copied_sub = original_sub
        else:
            original_prime, copied_prime = self._copy_and_modify_node_for_split(
                original_prime, variable, current_depth, max_depth
            )
            original_sub, copied_sub = self._copy_and_modify_node_for_split(
                original_sub, variable, current_depth, max_depth)
        if copied_prime is not None and copied_sub is not None:
            copied_element = AndGate(copied_prime, copied_sub,
                                     copy.deepcopy(original_element.parameter))
            copied_element.splittable_variables = copy.deepcopy(
                original_element.splittable_variables)
        else:
            copied_element = None
        if original_prime is not None and original_sub is not None:
            original_element.prime = original_prime
            original_element.sub = original_sub
        else:
            original_element = None
        return original_element, copied_element

    # Identical to LogisticCircuit
    def _copy_and_modify_node_for_split(self, original_node: AndChildNode, variable: int, current_depth: int,
                                        max_depth: int) -> Tuple[Optional[AndChildNode], Optional[AndChildNode]]:
        if original_node.num_parents == 0:
            raise ValueError("Some node does not have a parent.")
        original_node.decrease_num_parents_by_one()
        copied_node: Optional[AndChildNode]
        if isinstance(original_node, CircuitTerminal):
            if original_node.var_index == variable:
                if original_node.var_value == LITERAL_IS_TRUE:
                    copied_node = None
                elif original_node.var_value == LITERAL_IS_FALSE:
                    original_node = None
                    copied_node = self._terminal_nodes[self._num_variables + variable - 1]
                else:
                    raise ValueError(
                        "Under the current setting,"
                        "we only support terminal nodes that are either positive or negative literals."
                    )
            else:
                copied_node = original_node
            return original_node, copied_node
        else:
            if original_node.num_parents > 0:
                original_node = self._deep_copy_node(
                    original_node, variable, current_depth, max_depth)
            copied_elements = []
            i = 0
            while i < len(original_node.elements):
                original_element, copied_element = self._copy_and_modify_element_for_split(
                    original_node.elements[i], variable, current_depth + 1, max_depth
                )
                if original_element is None:
                    original_node.remove_element(i)
                else:
                    i += 1
                if copied_element is not None:
                    copied_elements.append(copied_element)
            if len(copied_elements) == 0:
                copied_node = None
            else:
                self._largest_index += 1
                copied_node = OrGate(self._largest_index, original_node.vtree, copied_elements)
            if len(original_node.elements) == 0:
                original_node = None
            return original_node, copied_node

    # Identical to LogisticCircuit
    def _deep_copy_node(self, node: AndChildNode, variable: int, current_depth: int, max_depth: int) -> AndChildNode:
        if isinstance(node, CircuitTerminal):
            return node
        else:
            if len(node.elements) == 0:
                raise ValueError("Decision nodes should have at least one elements.")
            copied_elements = []
            for element in node.elements:
                copied_elements.append(self._deep_copy_element(
                    element, variable, current_depth + 1, max_depth))
            self._largest_index += 1
            return OrGate(self._largest_index, node.vtree, copied_elements)

    # Identical to LogisticCircuit
    def _deep_copy_element(self, element: AndGate, variable: int, current_depth: int, max_depth: int) -> AndGate:
        copied_element: AndGate
        if current_depth >= max_depth:
            if variable in element.prime.vtree.variables:
                copied_element = AndGate(
                    self._deep_copy_node(element.prime, variable, current_depth, max_depth),
                    element.sub,
                    element.parameter.clone(),  # copy.deepcopy(element.parameter),
                )
            elif variable in element.sub.vtree.variables:
                copied_element = AndGate(
                    element.prime,
                    self._deep_copy_node(element.sub, variable, current_depth, max_depth),
                    element.parameter.clone(),  # copy.deepcopy(element.parameter),
                )
            else:
                copied_element = AndGate(element.prime, element.sub,
                                         element.parameter.clone(),  # copy.deepcopy(element.parameter)
                                         )
        else:
            copied_element = AndGate(
                self._deep_copy_node(element.prime, variable, current_depth, max_depth),
                self._deep_copy_node(element.sub, variable, current_depth, max_depth),
                element.parameter.clone(),  # copy.deepcopy(element.parameter),
            )
        copied_element.splittable_variables = copy.deepcopy(element.splittable_variables)
        return copied_element

    # def calculate_accuracy(self, data):
    #     """Calculate accuracy given the learned parameters on the provided data."""
    #     y = self.predict(data.features)
    #     accuracy = np.sum(y == data.labels) / data.num_samples
    #     return accuracy

    def calculate_error(self, data: DataSet) -> float:
        """Calculate accuracy given the learned parameters on the provided data."""
        y = self.predict(data.features)
        mse = mean_squared_error(data.labels, y)
        return mse

    # def predict(self, features):
    #     y = self.predict_prob(features)
    #     return np.argmax(y, axis=1)

    def predict(self, features: np.ndarray) -> torch.Tensor:
        return torch.mm(torch.from_numpy(features), self._parameters.T)

    # def predict_prob(self, features):
    #     """Predict the given images by providing their corresponding features."""
    #     y = 1.0 / (1.0 + np.exp(-np.dot(features, self._parameters.T)))
    #     return y

    def learn_parameters(self, data: DataSet, num_iterations: int, num_cores: int = -1,
                         alpha: float = 1.0, alpha_1: float = 1e-6, alpha_2: float = 1e-6,
                         tol: float = 0.001, solver: str = "auto",
                         rand_gen: Union[int, RandomState, None] = None) -> NoReturn:
        """Logistic Psdd's parameter learning is reduced to logistic regression.
        We use mini-batch SGD to optimize the parameters."""

        if solver == 'bayesian-ridge':
            model = BayesianRidge(
                alpha_1=alpha_1, alpha_2=alpha_2,
                fit_intercept=False,
                normalize=False,
                copy_X=True,
                # max_iter=num_iterations,
                tol=tol,
                coef=self._parameters.flatten().numpy(),
                # random_state=rand_gen, TODO?
            )
        elif solver == 'bayesian-ard':
            model = ARDRegression(
                alpha_1=alpha_1, alpha_2=alpha_2,
                fit_intercept=False,
                normalize=False,
                copy_X=True,
                # max_iter=num_iterations,
                tol=tol,
                coef=self._parameters.flatten().numpy(),
                # random_state=rand_gen, TODO?
            )
        # default to ridge and pass along solver
        else:
            model = Ridge(
                alpha=alpha,
                fit_intercept=False,
                normalize=False,
                copy_X=True,
                max_iter=num_iterations,
                tol=tol,
                solver=solver,
                # coef_=self._parameters,
                random_state=rand_gen,
            )

        print("About to fit model")
        model.fit(data.features, data.labels.numpy())
        # bayesian variants store the covariance
        if solver in ('bayesian-ridge', 'bayesian-ard'):
            print("Covariance:", np.sum(model.sigma_), np.shape(model.sigma_))
            self._covariance = [model.sigma_]
        # print('PARAMS', self._parameters.shape, model.coef_.shape)

        self._record_learned_parameters(model.coef_)
        gc.collect()

    def change_structure(self, data: DataSet, depth: int, num_splits: int, alpha: float) -> NoReturn:
        splits: List[Tuple[AndGate, int]]  = self._select_element_and_variable_to_split(data, num_splits, alpha)
        # print(len(splits), 'SPLITS')
        for element_to_split, variable_to_split in splits:
            if not element_to_split.flag:
                self._split(element_to_split, variable_to_split, depth)
        self._serialize()

    # Changes one string from LogisticCircuit
    def save(self, f: TextIO) -> NoReturn:
        self._serialize()
        f.write(FORMAT)
        f.write(f"Regression Circuit\n")
        for terminal_node in self._terminal_nodes:
            terminal_node.save(f)
        for decision_node in reversed(self._decision_nodes):
            decision_node.save(f)
        f.write("B")
        for parameter in self._bias:
            f.write(f" {parameter}")
        f.write("\n")
        if self._covariance is not None:
            for covMatrix in self._covariance:
                f.write("V")
                for cov in covMatrix.flatten():
                    f.write(f" {cov}")
                f.write("\n")

    # Bit different, should compare if considering merging
    def load(self, f: TextIO) -> OrGate:
        # read the format at the beginning
        line = f.readline()
        while line[0] == "c":
            line = f.readline()

        # serialize the vtree
        vtree_nodes: Dict[int, Vtree] = dict()
        unvisited_vtree_nodes: deque[Vtree] = deque()
        unvisited_vtree_nodes.append(self._vtree)
        while len(unvisited_vtree_nodes):
            node: Vtree = unvisited_vtree_nodes.popleft()
            vtree_nodes[node.index] = node
            if not node.is_leaf():
                unvisited_vtree_nodes.append(node.left)
                unvisited_vtree_nodes.append(node.right)

        # extract the saved logistic circuit
        terminal_nodes: Optional[Dict[int, Tuple[CircuitTerminal, Set[int]]]]  = dict()
        line = f.readline()
        while line[0] == "T" or line[0] == "F":
            line_as_list = line.strip().split(" ")
            positive_literal, var = (line_as_list[0] == "T"), int(line_as_list[3])
            index, vtree_index = int(line_as_list[1]), int(line_as_list[2])
            parameters: List[float] = []
            # for i in range(self._num_classes):
            # parameters.append(float(line_as_list[4 + i]))
            parameters.append(float(line_as_list[4]))
            if positive_literal:
                terminal_nodes[index] = (
                    CircuitTerminal(index, vtree_nodes[vtree_index], var, LITERAL_IS_TRUE,
                                    torch.tensor(parameters, dtype=torch.float64)),
                    {var}
                )
            else:
                terminal_nodes[index] = (
                    CircuitTerminal(index, vtree_nodes[vtree_index], var, LITERAL_IS_FALSE,
                                    torch.tensor(parameters, dtype=torch.float64)),
                    {-var})
            self._largest_index = max(self._largest_index, index)
            line = f.readline()

        self._terminal_nodes = [x[0] for x in terminal_nodes.values()]
        self._terminal_nodes.sort(key=lambda x: (-x.var_value, x.var_index))

        if len(self._terminal_nodes) != 2 * self._num_variables:
            raise ValueError(
                "Number of terminal nodes recorded in the circuit file "
                "does not match 2 * number of variables in the provided vtree."
            )

        # Broaden type hints from circuit terminal to circuit node
        nodes: Dict[int, Tuple[AndChildNode, Set[int]]] = terminal_nodes
        terminal_nodes = None

        root: Optional[OrGate] = None
        while line[0] == "D":
            line_as_list = line.strip().split(" ")
            index, vtree_index, num_elements = int(line_as_list[1]), int(line_as_list[2]), int(line_as_list[3])
            elements: List[AndGate] = []
            variables: Set[int] = set()
            for i in range(num_elements):
                # prime_index = int(line_as_list[i * (self._num_classes + 2) + 4].strip('('))
                # sub_index = int(line_as_list[i * (self._num_classes + 2) + 5])
                #
                # FIXME: remove constants
                prime_index = int(line_as_list[i * (1 + 2) + 4].strip("("))
                sub_index = int(line_as_list[i * (1 + 2) + 5])
                element_variables = nodes[prime_index][1].union(nodes[sub_index][1])
                variables = variables.union(element_variables)
                splittable_variables: Set[int] = set()
                for variable in element_variables:
                    if -variable in element_variables:
                        splittable_variables.add(abs(variable))
                parameters: List[float] = []
                # for j in range(self._num_classes):
                #     parameters.append(
                #         float(line_as_list[i * (self._num_classes + 2) + 6 + j].strip(')')))
                parameters.append(float(line_as_list[i * (1 + 2) + 6].strip(")")))
                elements.append(AndGate(nodes[prime_index][0], nodes[sub_index][0], torch.tensor(parameters, dtype=torch.float64)))
                elements[-1].splittable_variables = splittable_variables
            root = OrGate(index, vtree_nodes[vtree_index], elements)
            nodes[index] = (root, variables)
            self._largest_index = max(self._largest_index, index)
            line = f.readline()

        # Ensure the file contained at least one decision node
        if root is None:
            raise ValueError("Circuit must have at least one decision node to represent the root node")

        if line[0] != "B":
            raise ValueError("The last line in a circuit file must record the bias parameters.")
        self._bias = torch.tensor([float(x) for x in line.strip().split(" ")[1:]], dtype=torch.float64)

        # the parameters vector will be reconstructed after loading, but the covariance will need to be read
        line = f.readline()
        covariances: List[np.ndarray] = []
        while line and line[0] == "V":
            vector = np.array([float(x) for x in line.strip().split(" ")[1:]], dtype=np.float64)
            matrixSize: Union[int, float] = math.sqrt(vector.size)
            if not matrixSize.is_integer():
                print("Error: covariance matrix must be square")
                # raise ValueError("Covariance matrix must be square"
            else:
                matrixSize = int(matrixSize)
                covariances.append(vector.reshape((matrixSize, matrixSize)))
            line = f.readline()
        self._covariance = covariances

        del nodes
        gc.collect()
        return root


def learn_regression_circuit(
    vtree: Vtree,
    train: DataSet,
    valid: Optional[DataSet] = None,
    solver: str = "auto",
    max_iter_sl: int = 1000,
    max_iter_pl: int = 1000,
    depth: int = 20,
    num_splits: int = 10,
    alpha: float = 0.2,
    validate_every: int = 10,
    patience: int = 2,
    rand_gen: Optional[RandomState] = None
) -> Tuple[RegressionCircuit, List[float]]:

    # #
    # # FIXEME: do we need this?
    # X[np.where(X == 0.0)[0]] = 1e-5
    # X[np.where(X == 1.0)[0]] -= 1e-5

    # train = Dataset(train_x, train_y)

    error_history: List[float] = []

    circuit = RegressionCircuit(vtree, rand_gen=rand_gen)
    train.features = circuit.calculate_features(train.images)

    logging.info(f"The starting circuit has {circuit.num_parameters} parameters.")
    train.features = circuit.calculate_features(train.images)
    train_acc: float = circuit.calculate_error(train)
    logging.info(f" error: {train_acc:.5f}")
    error_history.append(train_acc)

    logging.info("Start structure learning.")
    sl_start_t: float = perf_counter()

    valid_best = +np.inf
    best_model = copy.deepcopy(circuit)
    c_patience = 0
    for i in range(max_iter_sl):
        cur_time = perf_counter()

        circuit.change_structure(train, depth, num_splits, alpha)

        train.features = circuit.calculate_features(train.images)
        pl_start_t = perf_counter()
        circuit.learn_parameters(train, max_iter_pl, rand_gen=rand_gen, solver=solver)
        pl_end_t = perf_counter()

        train_acc: float = circuit.calculate_error(train)

        logging.info(f"done iter {i+1}/{max_iter_sl} in {perf_counter() - cur_time} secs")
        logging.info(f"\tcircuit size: {circuit.num_parameters}")
        logging.info(f"\tparameters learning done in {pl_end_t - pl_start_t} secs")
        logging.info(f"\terror: {train_acc:.5f} size {circuit.num_parameters}")
        error_history.append(train_acc)

        if i % validate_every == 0 and valid is not None:
            logging.info(f"evaluate in the validation set")
            valid.features = circuit.calculate_features(valid.images)
            valid_err = circuit.calculate_error(valid)
            if valid_err >= valid_best:
                logging.info(f"Worsening on valid: {valid_err} > prev best {valid_best}")
                if c_patience >= patience:
                    logging.info(f"Exceeding patience {c_patience} >= {patience}: STOP")
                    break
                else:
                    c_patience += 1
            else:
                logging.info(f'Found new best model {valid_err}')
                best_model = copy.deepcopy(circuit)
                valid_best = valid_err
                c_patience = 0

    sl_end_t: float = perf_counter()
    logging.info(f"Structure learning done in {sl_end_t - sl_start_t} secs")

    return best_model, error_history
