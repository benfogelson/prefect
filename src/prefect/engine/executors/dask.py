# Licensed under LICENSE.md; also available at https://www.prefect.io/licenses/alpha-eula

import datetime
from contextlib import contextmanager
from distributed import Client, Queue, worker_client
from typing import Any, Callable, Iterable

import dask
import dask.bag
import queue
import warnings

from prefect.engine.executors.base import Executor
from prefect.utilities.executors import (
    create_bagged_list,
    dict_to_list,
    state_to_list,
    unpack_dict_to_bag,
)


class DaskExecutor(Executor):
    """
    An executor that runs all functions using the `dask.distributed` scheduler on
    a local dask cluster.  If you already have one running, simply provide the
    address of the scheduler upon initialization; otherwise, one will be created
    (and subsequently torn down) within the `start()` contextmanager.

    Args:
        - address (string, optional): address of a currently running dask
            scheduler; defaults to `None`
        - processes (bool, optional): whether to use multiprocessing or not
            (computations will still be multithreaded). Ignored if address is provided.
            Defaults to `False`.
        - **kwargs (dict, optional): additional kwargs to be passed to the
            `dask.distributed.Client` upon initialization (e.g., `n_workers`)
    """

    def __init__(self, address=None, processes=False, **kwargs):
        self.address = address
        self.processes = processes
        self.kwargs = kwargs
        super().__init__()

    @contextmanager
    def start(self) -> Iterable[None]:
        """
        Context manager for initializing execution.

        Creates a `dask.distributed.Client` and yields it.
        """
        try:
            with Client(
                self.address, processes=self.processes, **self.kwargs
            ) as client:
                self.client = client
                yield self.client
        finally:
            self.client = None

    def queue(self, maxsize=0, client=None):
        """
        Creates an executor-compatible Queue object which can share state
        across tasks.

        Args:
            - maxsize (int, optional): `maxsize` for the Queue; defaults to 0
                (interpreted as no size limitation)
            - client (dask.distributed.Client, optional): which client to
                associate the Queue with
        """
        q = Queue(maxsize=maxsize, client=client or self.client)
        return q

    def map(
        self, fn: Callable, *args: Any, maps=None, upstream_states=None, **kwargs: Any
    ):
        def mapper(maps, fn, *args, upstream_states, **kwargs):
            non_keyed = upstream_states.pop(None, [])
            states = dict_to_list(maps)

            with worker_client() as client:
                futures = []
                for elem in states:
                    upstream_states.update(elem)
                    upstream_states[None] = (
                        list(upstream_states.get(None, [])) + non_keyed
                    )
                    futures.append(
                        client.submit(
                            fn, *args, upstream_states=upstream_states, **kwargs
                        )
                    )
            return futures

        future_list = self.client.submit(
            mapper, maps, fn, *args, upstream_states=upstream_states, **kwargs
        )
        ## gather is needed simply to convert the future_list -> list
        ## since more futures were submitted within the function
        ## this will not block for the futures contained in that list
        return self.client.gather(future_list)

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> dask.delayed:
        """
        Submit a function to the executor for execution. Returns a Future object.

        Args:
            - fn (Callable): function which is being submitted for execution
            - *args (Any): arguments to be passed to `fn`
            - context (dict): `prefect.utilities.Context` to be used in function execution
            - **kwargs (Any): keyword arguments to be passed to `fn`

        Returns:
            - Future: a Future-like object which represents the computation of `fn(*args, **kwargs)`
        """

        return self.client.submit(fn, *args, **kwargs, pure=False)

    def wait(self, futures: Iterable, timeout: datetime.timedelta = None) -> Iterable:
        """
        Resolves the Future objects to their values. Blocks until the computation is complete.

        Args:
            - futures (Iterable): iterable of future-like objects to compute
            - timeout (datetime.timedelta): maximum length of time to allow for
                execution

        Returns:
            - Iterable: an iterable of resolved futures
        """
        return self.client.gather(futures)
