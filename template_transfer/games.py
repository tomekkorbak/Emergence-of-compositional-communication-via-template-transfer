import random
from collections import defaultdict

import torch
import torch.nn as nn
import torch.nn.functional as F


def entangled_loss(targets, receiver_output_1, receiver_output_2):
    acc_1 = (receiver_output_1.argmax(dim=1) == targets[:, 0]).detach().float()
    acc_2 = (receiver_output_2.argmax(dim=1) == targets[:, 1]).detach().float()
    loss_1 = F.cross_entropy(receiver_output_1, targets[:, 0], reduction="none")
    loss_2 = F.cross_entropy(receiver_output_2, targets[:, 1], reduction="none")
    acc = (acc_1 * acc_2).mean(dim=0)
    loss = loss_1 + loss_2
    return loss, {f'accuracy': acc.item(),
                  f'first_accuracy': acc_1.mean(dim=0).item(),
                  f'second_accuracy': acc_2.mean(dim=0).item()}


def disentangled_loss(target, output, prefix):
    acc = (output.argmax(dim=1) == target).detach().float().mean(dim=0)
    loss = F.cross_entropy(output, target, reduction="none")
    return loss, {f'{prefix}_accuracy': acc.item()}


def sample(message_size):
    batch_size, _, vocab_size = message_size
    random_symbols = torch.randint(0, vocab_size, size=(batch_size,))
    return F.one_hot(random_symbols, num_classes=vocab_size).float().unsqueeze(dim=1)


def sample_reinforce(message_size, vocab_size):
    batch_size, _ = message_size
    random_symbols = torch.randint(0, vocab_size, size=(batch_size,))
    return random_symbols.unsqueeze(dim=1)


class PretrainingmGameGS(nn.Module):
    def __init__(
            self,
            senders,
            receiver,
    ):
        super(PretrainingmGameGS, self).__init__()
        self.sender_1, self.sender_2 = senders
        self.receiver = receiver

    def forward(self, sender_input, target):
        message_1 = self.sender_1(sender_input)
        message_2 = sample(message_1.size())
        message = torch.cat([message_1, message_2], dim=1)
        first_receiver_output, second_receiver_output = self.receiver(message)
        loss_1, rest_info_1 = disentangled_loss(target[:, 0], first_receiver_output[:, -1, ...], prefix='first')

        message_2 = self.sender_2(sender_input)
        message_1 = sample(message_2.size())
        message = torch.cat([message_1, message_2], dim=1)
        first_receiver_output, second_receiver_output = self.receiver(message)
        loss_2, rest_info_2 = disentangled_loss(target[:, 1], second_receiver_output[:, -1, ...], prefix='second')

        rest = {
            'first_accuracy': rest_info_1['first_accuracy'],
            'second_accuracy': rest_info_2['second_accuracy'],
            'accuracy': (rest_info_1['first_accuracy'] + rest_info_2['second_accuracy'])/2
        }
        return (loss_1 + loss_2).mean(), rest


class CompositionalGameGS(nn.Module):
    def __init__(
            self,
            sender,
            receiver,
    ):
        super(CompositionalGameGS, self).__init__()
        self.sender = sender
        self.receiver = receiver

    def forward(self, sender_input, target):
        message = self.sender(sender_input)
        first_receiver_output, second_receiver_output = self.receiver(message)
        loss, rest_info = entangled_loss(target, first_receiver_output[:, -1, ...], second_receiver_output[:, -1, ...])
        return loss.mean(), rest_info


class PretrainingmGameReinforce(nn.Module):
    def __init__(
            self,
            senders,
            receiver,
    ):
        super(PretrainingmGameReinforce, self).__init__()
        self.sender_1, self.sender_2 = senders
        self.receiver = receiver
        self.sender_entropy_coeff = 0.01

        self.mean_baseline = defaultdict(float)
        self.n_points = defaultdict(float)

    def forward(self, sender_input, target):
        if random.choice([True, False]):
            agent = 'first'
            message_1, log_probs, entropy = self.sender_1(sender_input)
            message_2 = sample_reinforce(message_1.size(), vocab_size=self.sender_1.vocab_size)
            message = torch.cat([message_1, message_2], dim=1)
            first_receiver_output, second_receiver_output = self.receiver(message)
            loss, rest_info = disentangled_loss(target[:, 0], first_receiver_output, prefix=agent)

        else:
            agent = 'second'
            message_2, log_probs, entropy = self.sender_2(sender_input)
            message_1 = sample_reinforce(message_2.size(), vocab_size=self.sender_2.vocab_size)
            message = torch.cat([message_1, message_2], dim=1)
            first_receiver_output, second_receiver_output = self.receiver(message)
            loss, rest_info = disentangled_loss(target[:, 1], second_receiver_output, prefix=agent)

        policy_loss = ((loss.detach() - self.mean_baseline[agent]) * log_probs).mean()
        entropy_loss = -entropy.mean() * self.sender_entropy_coeff

        if self.training:
            self.update_baseline(agent, loss)

        full_loss = policy_loss + entropy_loss + loss.mean()

        rest_info['baseline_' + agent] = self.mean_baseline[agent]
        rest_info['loss'] = loss.mean().item()
        rest_info['accuracy'] = rest_info[agent + '_accuracy']
        rest_info['sender_entropy'] = entropy.mean().item()
        return full_loss.mean(), rest_info

    def update_baseline(self, name, value):
        self.n_points[name] += 1
        self.mean_baseline[name] += (value.detach().mean().item() - self.mean_baseline[name]) / self.n_points[name]


class CompositionalGameReinforce(nn.Module):
    def __init__(
            self,
            sender,
            receiver,
    ):
        super(CompositionalGameReinforce, self).__init__()
        self.sender = sender
        self.receiver = receiver
        self.sender_entropy_coeff = 0.2

        self.mean_baseline = 0.0
        self.n_points = 0.0

    def forward(self, sender_input, target):
        message, log_probs, entropy = self.sender(sender_input)
        first_receiver_output, second_receiver_output = self.receiver(message)
        loss, rest_info = entangled_loss(target, first_receiver_output, second_receiver_output)

        policy_loss = ((loss.detach() - self.mean_baseline) * log_probs.sum(dim=1)).mean()
        entropy_loss = -entropy.mean() * self.sender_entropy_coeff

        if self.training:
            self.n_points += 1.0
            self.mean_baseline += (loss.detach().mean().item() - self.mean_baseline) / self.n_points

        full_loss = policy_loss + entropy_loss + loss.mean()

        rest_info['baseline'] = self.mean_baseline
        rest_info['loss'] = loss.mean().item()
        rest_info['sender_entropy'] = entropy.mean().item()
        return full_loss.mean(), rest_info