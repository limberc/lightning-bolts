import math

import numpy as np
from pytorch_lightning import LightningDataModule
from torch.utils.data import DataLoader, SequentialSampler

from pl_bolts.models.self_supervised.fixmatch.transforms import RandAugmentMC
from pl_bolts.transforms.dataset_normalizations import cifar10_normalization, cifar100_normalization
from pl_bolts.utils import _TORCHVISION_AVAILABLE
from pl_bolts.utils.warnings import warn_missing_pkg

if _TORCHVISION_AVAILABLE:
    from torchvision import datasets, transforms
else:  # pragma: no cover
    warn_missing_pkg("torchvision")

TRANS_WEAK = transforms.Compose(
    [
        transforms.RandomHorizontalFlip(),
        transforms.RandomCrop(32, padding=int(32 * 0.125), padding_mode="reflect"),
    ]
)

TRANS_STRONG = transforms.Compose(
    [
        transforms.RandomHorizontalFlip(),
        transforms.RandomCrop(32, padding=int(32 * 0.125), padding_mode="reflect"),
        RandAugmentMC(n=2, m=10),
    ]
)
TRANS_STRONG_ANOTHER = transforms.Compose(
    [
        transforms.RandomResizedCrop(32, scale=(0.2, 1.0)),
        transforms.RandomHorizontalFlip(p=0.5),
        transforms.RandomApply([transforms.ColorJitter(0.4, 0.4, 0.4, 0.1)], p=0.8),
        transforms.RandomGrayscale(p=0.2),
    ]
)


class TransformSSL:
    def __init__(self, dataset, mode="fixmatch"):
        self.test_transforms = transforms.Compose([transforms.Resize(32)])
        self.weak = TRANS_WEAK
        self.strong1 = TRANS_STRONG
        self.strong2 = TRANS_STRONG_ANOTHER
        self.mode = mode
        norm = self.DATASET_NORMS[dataset]()
        self.normalize = transforms.Compose([transforms.ToTensor(), norm])

    def __call__(self, x):
        if self.mode == "test":
            test = self.test_transforms(x)
            return self.normalize(test)
        weak = self.weak(x)
        if self.mode == "casual":
            return self.normalize(weak)
        strong1 = self.strong1(x)
        if self.mode == "fixmatch":
            return self.normalize(weak), self.normalize(strong1)
        strong2 = self.strong2(x)
        return self.normalize(weak), self.normalize(strong1), self.normalize(strong2)


class CIFAR10SSL(datasets.CIFAR10):
    def __init__(self, root, indexs, train=True, transform=None, target_transform=None, download=False):
        super().__init__(root, train=train, transform=transform, target_transform=target_transform, download=download)
        if indexs is not None:
            self.data = self.data[indexs]
            self.targets = np.array(self.targets)[indexs]


class CIFAR100SSL(datasets.CIFAR100):
    def __init__(self, root, indexs, train=True, transform=None, target_transform=None, download=False):
        super().__init__(root, train=train, transform=transform, target_transform=target_transform, download=download)
        if indexs is not None:
            self.data = self.data[indexs]
            self.targets = np.array(self.targets)[indexs]


MAP_DATASET = {"cifar10": datasets.CIFAR10, "cifar100": datasets.CIFAR100}
MAP_SSL_DATASET = {"cifar10": CIFAR10SSL, "cifar100": CIFAR100SSL}


def x_u_split(dataset, labels, num_labeled=4000, eval_step=1024, expand_labels=True, batch_size=128):
    if dataset == "cifar10":
        num_classes = 10
    elif dataset == "cifar100":
        num_classes = 100
    label_per_class = num_labeled // num_classes
    labels = np.array(labels)
    labeled_idx = []
    # unlabeled data: all data (https://github.com/kekmodel/FixMatch-pytorch/issues/10)
    unlabeled_idx = np.array(range(len(labels)))
    for i in range(num_classes):
        idx = np.where(labels == i)[0]
        idx = np.random.choice(idx, label_per_class, False)
        labeled_idx.extend(idx)
    labeled_idx = np.array(labeled_idx)
    assert len(labeled_idx) == num_labeled

    if expand_labels or num_labeled < batch_size:
        num_expand_x = math.ceil(batch_size * eval_step / num_labeled)
        labeled_idx = np.hstack([labeled_idx for _ in range(num_expand_x)])
    np.random.shuffle(labeled_idx)
    return labeled_idx, unlabeled_idx


def get_train_dataset(
    data_path, dataset, mode="fixmatch", num_labeled=4000, batch_size=128, eval_step=1024, expand_labels=True
):
    assert mode in ["fixmatch", "comatch"]
    base_dataset = MAP_DATASET[dataset](data_path, train=True, download=True)
    train_labeled_idxs, train_unlabeled_idxs = x_u_split(
        dataset, base_dataset.targets, num_labeled, eval_step, expand_labels, batch_size
    )
    train_labeled_dataset = MAP_SSL_DATASET[dataset](
        data_path, train_labeled_idxs, train=True, transform=TransformSSL(dataset, "casual")
    )
    train_unlabeled_dataset = MAP_SSL_DATASET[dataset](
        data_path, train_unlabeled_idxs, train=True, transform=TransformSSL(dataset, mode)
    )
    return train_labeled_dataset, train_unlabeled_dataset


class SSLDataModule(LightningDataModule):
    def __init__(
        self,
        data_path,
        dataset,
        mu=7,
        mode="fixmatch",
        num_labeled=4000,
        batch_size=128,
        eval_step=1024,
        expand_labels=True,
    ):
        super().__init__()
        self.batch_size = batch_size
        self.mu = mu
        self.data_path = data_path
        self.dataset = dataset
        self.mode = mode
        self.num_labeled = num_labeled
        self.eval_step = eval_step
        self.expand_labels = expand_labels

    def prepare_data(self):
        self.test_dataset = MAP_DATASET[self.dataset](
            self.data_path, train=False, transform=TransformSSL(self.dataset, "test")
        )
        self.train_labeled_dataset, self.train_unlabeled_dataset = get_train_dataset(
            self.data_path,
            self.dataset,
            self.mode,
            self.num_labeled,
            self.batch_size,
            self.eval_step,
            self.expand_labels,
        )

    def train_dataloader(self):
        labeled_loader = DataLoader(
            self.train_labeled_dataset, batch_size=self.batch_size, pin_memory=True, num_workers=16, drop_last=True
        )
        unlabeled_loader = DataLoader(
            self.train_unlabeled_dataset,
            batch_size=self.batch_size * self.mu,
            pin_memory=True,
            num_workers=16,
            drop_last=True,
        )
        return {"labeled": labeled_loader, "unlabeled": unlabeled_loader}

    def val_dataloader(self):
        return DataLoader(
            self.test_dataset, batch_size=self.batch_size, num_workers=8, sampler=SequentialSampler(self.test_dataset)
        )


if __name__ == "__main__":
    dm = SSLDataModule("./data", "cifar100")
    train_loader = dm.train_dataloader()
    print(train_loader.keys())
