# Copyright 2023 The DLRover Authors. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import time
from abc import ABCMeta, abstractmethod

import torch
import torch.distributed as dist

from dlrover.python.common import env_utils
from dlrover.python.common.log import default_logger as logger
from dlrover.python.common.multi_process import SharedLock, SharedQueue
from dlrover.python.elastic_agent.torch.ckpt_saver import (
    DLROVER_CKPT_CONFIG_KEY,
    CheckpointEvent,
    CheckpointEventType,
    CheckpointShardConfig,
    CheckpointSharedObjPrefix,
    SaverClassMeta,
    SharedMemoryHandler,
    SingleFileCheckpointConfig,
)


def check_all_rank_ready(group: dist.ProcessGroup, ready):
    """
    Check weather all ranks are ready.
    """
    if not group:
        return ready
    value = 0 if ready else 1
    t = torch.tensor([value], dtype=torch.int64)
    dist.all_reduce(t, group=group)
    return t == 0


def verify_all_rank_step_consistent(group: dist.ProcessGroup, step):
    """
    Verify wether the step in all ranks are consistent.
    """
    if not group:
        return True
    t = torch.Tensor([float(step)])
    world_size = group.size()
    outputs = [torch.Tensor([0.0]) for _ in range(world_size)]
    dist.all_gather(outputs, t, group=group)
    for step in outputs:
        if not torch.equal(step, outputs[0]):
            return False
    return True


def timer(func):
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        t = round(time.time() - start, 3)
        logger.info(f"Function {func.__name__} cost {t}s")
        return result

    return wrapper


class CheckpointEngine(metaclass=ABCMeta):
    """
    The checkpoint engine synchronously writes the state dict into
    the shared memory and notify the agent in main process to
    asynchronously save the state dict from the shared memory into
    the storage. Writing to memory is significantly quicker
    than writing to storage. The engine only blocks the training
    with a little time. Users can frequently call `save_to_memory` in
    the training loop and call `save_to_storage`.

    If the training process fail, the agent in main process can continuely
    saves the the state dict from the shared memory into the storage.

    Args:
        checkpoint_dir (str): the directory to save checkpoint.
    """

    def __init__(self, checkpoint_dir: str):
        self.checkpoint_dir = checkpoint_dir
        if dist.is_initialized():
            self._rank = dist.get_rank()
        else:
            self._rank = 0
        self._local_rank = int(os.getenv("LOCAL_RANK", 0))
        self._saver_group = None
        self._cached_step = 0
        self._restart_count = env_utils.get_torch_restart_count()
        # queue for agent to save to storage, only rank 0
        if self._rank == 0:
            self._event_queue = SharedQueue(
                name=CheckpointSharedObjPrefix.SAVE_STEP_QNAME + str(0),
                create=False,
            )
        else:
            self._event_queue = None  # type: ignore
        # lock for shared memory
        local_shard_num = self.get_local_shard_num()
        self.local_shard_id = self._local_rank % local_shard_num
        lock_name = CheckpointSharedObjPrefix.SHM_LOCK_NAME + str(
            self.local_shard_id
        )
        self._shm_lock = SharedLock(name=lock_name, create=False)
        self._shm_handler = SharedMemoryHandler(
            self.local_shard_id, host=False
        )
        self._notify_agent_to_create_saver()
        self._update_saver_config()

    def __del__(self):
        self.close()

    def close(self):
        """Close the shared memory."""
        self._shm_handler.close()

    def _notify_agent_to_create_saver(self):
        """Notify the agent in the main process to create a checkpoint saver"""
        if self._local_rank != 0:
            return
        if self._restart_count > 0:
            # Only local rank 0 notify to initialize the saver in
            # the main process at the first start.
            # Avoid the lock is locked by a failed process.
            self._shm_lock.release()
            return
        queue = SharedQueue(name="factory")

        local_shard_num = self.get_local_shard_num()
        global_shard_num = self.get_global_shard_num()
        clazz = self.get_saver_class()
        class_meta = SaverClassMeta(
            module_path=clazz.__module__,
            class_name=clazz.__name__,
            init_args={
                "checkpoint_dir": self.checkpoint_dir,
                "local_shard_num": local_shard_num,
                "global_shard_num": global_shard_num,
            },
        )

        queue.put(class_meta)
        queue.unlink()

    def _update_saver_config(self):
        """Update the sharding configuration to the saver."""
        if self._local_rank == 0:
            global_shard_num = self.get_global_shard_num()
            event: CheckpointEvent = CheckpointEvent(
                type=CheckpointEventType.UPDATE_SHARD,
                global_shard_num=global_shard_num,
            )
            self._event_queue.put(event)

    @timer
    def save_to_memory(self, step, state_dict, path=""):
        """
        Synchronously Saves the state dict into the shared memory with the main
        process. If the agent in the main process is saving the shared memory
        into the storage, the method will skip to write the shared memory.
        Only local rank 0 save the state dict into the memory because the
        state dict is replicated across all ranks.

        Args:
            step (int): the global iteration step.
            state_dict (dict): the state dict of model and optimizer to save.
            path (str): the storage path to save the state dict.
                Note, the path is used to save the state dict to storage
                only if the training process fails.
        """
        conf = SingleFileCheckpointConfig(
            step=step,
            path=path,
        )
        self.save_state_dict_to_memory(state_dict, conf)

    def save_state_dict_to_memory(
        self, state_dict, conf: CheckpointShardConfig
    ):
        if self._local_rank != self.local_shard_id:
            return

        if DLROVER_CKPT_CONFIG_KEY in state_dict:
            raise ValueError(
                "The state_dict can not have the key "
                f"{DLROVER_CKPT_CONFIG_KEY}."
            )

        acquired = self._shm_lock.acquire(blocking=False)
        all_rank_ready = check_all_rank_ready(self._saver_group, acquired)
        if not all_rank_ready:
            logger.info(
                f"Rank {self._rank} skips the save the checkpoint "
                f"in CPU memory since it is saving the latest "
                "checkpoint from the CPU memory into the storage."
            )
            if acquired:
                self._shm_lock.release()
            return
        self._shm_handler.save_state_dict(state_dict, conf)

        if acquired:
            self._shm_lock.release()
        self._cached_step = conf.step
        if dist.is_initialized():
            dist.barrier(group=self._saver_group)

    def get_state_dict_from_memory(self):
        state_dict = {}
        default_config = CheckpointShardConfig()
        config = self._shm_handler.get_checkpoint_config(default_config)
        if config.step == 0:
            return state_dict
        passed = verify_all_rank_step_consistent(
            self._saver_group, config.step
        )
        if passed:
            state_dict = self._shm_handler.load_state_dict()
            logger.info(
                f"Load step {config.step} checkpoint from the shared memory."
            )
        return state_dict

    @abstractmethod
    def get_saver_class(self):
        """
        Get a CheckpointSaver class.
        """
        pass

    @abstractmethod
    def get_local_shard_num(self):
        """Get the number of model shards on the node."""
        pass

    @abstractmethod
    def get_global_shard_num(self):
        """Get the number of model shards on all nodes."""
        pass

    @abstractmethod
    def save_to_storage(self, step, state_dict, path):
        """
        Save the state_dict into the path of storage.

        Args:
            step (int): the iteration step.
            state_dict (dict): the state dict of model and optimizer to save.
            path (str): optional, the file path to save the checkpoint. If the
                path is not defined, the engine will save the state dict into
                the shared memory not the storage.
        """
        pass

    @abstractmethod
    def load(self, resume_path=""):
        """
        Load the checkpointing state dict from the resume path.

        Returns:
            A dict.
        """
        pass
