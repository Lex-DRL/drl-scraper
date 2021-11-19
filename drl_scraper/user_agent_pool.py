# encoding: utf-8
"""
The main class this module provides is ``UserAgentPool``, and the only method
that might be of interest to you is ``UserAgentPool.random`` property.

It randomly chooses one of user-agents from the pool, respecting usage stats of
real browsers.

The actual list of user-agent strings, with their market share - is in
``user_agent_pool.json`` config file, which should be updated periodically.
"""

__author__ = 'Lex Darlog (DRL)'

from itertools import (
	accumulate as _accumulate,
	chain as _chain,
)
import json as _json
from pathlib import Path as _Path
from random import choices as _rnd_choices

from common import StaticDataClass as _StaticDataClass

from drl_typing import *

_t_percent = _if


module_dir = _Path(__file__).parent


class UserAgentError(ValueError):
	pass


class UserAgent(_NT):
	user_agent: str
	system: str
	percent: _t_percent

	def sort_key(self):
		return -self.percent, self.user_agent


class UserAgentData(_NT):
	percent: _t_percent
	system: str = ''

	def ensure_values(self):
		"""Raise a UserAgentError if values are of a wrong type."""
		percent, system = self
		if not isinstance(percent, (int, float)) or percent == 0:
			raise UserAgentError(f'{self}: Not a valid percent: {percent!r}')
		if not (system is None or isinstance(system, str)):
			raise UserAgentError(f'{self}: Not a valid system: {system!r}')


def __ensure_ua_data(val) -> UserAgentData:
	if isinstance(val, UserAgentData):
		return val

	percent = system = None
	try:
		percent = val.percent
		system = val.system
	except AttributeError:
		ok = False
	else:
		ok = True

	if not ok:
		# noinspection PyBroadException
		try:
			percent, system, *_ = val
		except Exception:
			ok = False
		else:
			ok = True

	if ok:
		return UserAgentData(percent, system)
	raise UserAgentError(f'Not a UserAgentData nor a valid iterable: {val}')


def ensure_ua_data(val) -> UserAgentData:
	"""Raise a UserAgentError if the given value is not a valid UserAgentData."""
	res = __ensure_ua_data(val)
	res.ensure_values()
	return res


_t_pool = _d[_str, UserAgentData]
_t_pool_item = _tpl[_str, UserAgentData]


class _UserAgentPoolMeta(type):
	"""
	Meta-class used to be able to have static properties and access class as dict
	(user-agents as keys).

	The initial user-agent list is taken here:

	https://techblog.willshouse.com/2012/01/03/most-common-user-agents/
	https://www.google.com/search?q=list+of+user-agent+strings+by+usage
	"""
	pool_file = module_dir / 'user_agent_pool.json'

	__internal_pool: _t_pool = dict()

	__map_to_percent: _d[_str, _t_percent] = dict()
	__map_to_sys: _d[_str, _str] = dict()

	__flat_uas: _tpl_str = tuple()
	__flat_percents: _tpl[_t_percent, ...] = tuple()
	__flat_cumul_weights: _tpl[float, ...] = tuple()
	__flat_ids_per_key: _d[_str, int] = dict()

	@staticmethod
	def __ensure_item(item) -> _t_pool_item:
		# noinspection PyBroadException
		def error(val, extra=None):
			msg = f'Not a valid UserAgentPool key-value pair: {val!r}'
			if isinstance(extra, Exception):
				try:
					e_args = extra.args
				except Exception:
					e_args = []
				if e_args:
					try:
						extra = e_args[0] if len(e_args) == 1 else '\n'.join(
							f'{x}' for x in e_args
						)
					except Exception:
						extra = None
				else:
					extra = None
			if extra:
				msg = f'{msg}\n{extra}'
			return UserAgentError(msg)

		if not isinstance(item, _seq):
			raise error(item)
		try:
			k, v = item
		except Exception as e:
			raise error(item, e)
		else:
			pass
		if not isinstance(k, str):
			raise error(item)
		clean_k = k.strip()
		if not clean_k:
			raise error(item)
		k = clean_k
		item = (k, v)
		if not isinstance(v, UserAgentData):
			v = ensure_ua_data(v)
			item = (k, v)
		return item

	def __init_flat_tuples(cls):
		cls.__flat_uas: _tpl_str = tuple()
		cls.__flat_percents: _tpl[_t_percent, ...] = tuple()
		cls.__flat_cumul_weights: _tpl[float, ...] = tuple()
		cls.__flat_ids_per_key: _d[_str, int] = dict()

	def __pool_or_init(cls):
		try:
			return cls.__internal_pool
		except AttributeError:
			cls.__internal_pool: _t_pool = dict()
			cls.__map_to_percent: _d[_str, _t_percent] = dict()
			cls.__map_to_sys: _d[_str, _str] = dict()
			cls.__init_flat_tuples()
			return cls.__internal_pool

	def __pool_or_load(cls):
		pool = cls.__pool_or_init()
		if pool:
			return pool
		cls._load()
		return cls.__internal_pool

	@property
	def whole_pool(cls):
		return dict(cls.__pool_or_load())

	def __contains__(cls, key):
		return key in cls.__pool_or_load()

	def __getitem__(cls, key):
		pool = cls.__pool_or_load()
		if key not in pool:
			return None
		return pool[key]

	def __setitem__(cls, key: _str, value: _u[UserAgentData, _seq]):
		o_key, o_value = cls.__ensure_item((key, value))
		cls.__pool_or_load()[o_key] = o_value
		p, s = o_value
		cls.__map_to_percent[o_key] = p
		cls.__map_to_sys[o_key] = s
		cls.__rebuild_flat_tuples(cls.__flat_pool(load=False, sort=True))

	def __delitem__(cls, key):
		cls.pop(key)

	def pop(cls, key):
		pool = cls.__pool_or_load()
		if key not in pool:
			return None
		res = pool.pop(key)
		# noinspection PyBroadException
		try:
			cls.__map_to_percent.pop(key)
			cls.__map_to_sys.pop(key)
			cls.__remove_from_flat_tuples(key)
		except Exception:
			# somehow we've got a key mismatch between dicts, let's recreate maps:
			cls.__recreate_internal_caches()
		return res

	def __remove_from_flat_tuples(cls, key):
		excluded_i = cls.__flat_ids_per_key.pop(key)
		if not cls.__flat_ids_per_key:
			cls.__init_flat_tuples()
			return
		next_i = excluded_i + 1

		def popped_tuple(tpl: _tpl[_T, ...]) -> _tpl[_T, ...]:
			return tuple(_chain(tpl[:excluded_i], tpl[next_i:]))

		cls.__flat_uas = popped_tuple(cls.__flat_uas)
		cls.__flat_percents = popped_tuple(cls.__flat_percents)
		cls.__rebuild_cumul_weights()

	def __rebuild_flat_tuples(cls, flat_sorted_pool: _seq[UserAgent]):
		if not flat_sorted_pool:
			cls.__init_flat_tuples()
			return

		# reverse sort, to make smaller percent values first - therefore,
		# more effectively use float precision:
		pool_low_to_big_percent = reversed(flat_sorted_pool)
		flat_ids, cls.__flat_uas, _systems, cls.__flat_percents, *_ = zip(*(
			(i, *data) for i, data in enumerate(pool_low_to_big_percent)
		))
		cls.__flat_ids_per_key = {ua_k: i for ua_k, i in zip(cls.__flat_uas, flat_ids)}
		cls.__rebuild_cumul_weights()

	def __rebuild_cumul_weights(cls):
		cumul_weights = tuple(_accumulate(cls.__flat_percents))
		# it's already a cumul tuple, but let's also normalize it (max = 1.0):
		cumul_scale = cumul_weights[-1]
		cumul_scale = 1.0 / cumul_scale if cumul_scale > 0 else 1.0
		cls.__flat_cumul_weights = tuple(float(x * cumul_scale) for x in cumul_weights)

	def __recreate_internal_caches(cls):
		flat_sorted_pool = cls.__flat_pool(load=False, sort=True)
		cls.__map_to_percent = {ua_k: pc for ua_k, sys, pc in flat_sorted_pool}
		cls.__map_to_sys = {ua_k: sys for ua_k, sys, pc in flat_sorted_pool}
		cls.__rebuild_flat_tuples(flat_sorted_pool)

	def update(cls, items: _i[_t_pool_item]):
		if not items:
			return
		pool = cls.__pool_or_init()
		pool.update(cls.__ensure_item(x) for x in items)
		cls.__recreate_internal_caches()

	def _load(cls):
		with open(cls.pool_file, 'rt', encoding='utf-8') as f:
			raw = _json.load(f)
		if not raw:
			raise UserAgentError(f"Unable to load {cls}: empty {cls.pool_file}")
		pool_flattened = cls.__flat_pool_sort(
			UserAgent(*x) for x in raw
		)
		cls.update(
			(ua_k, UserAgentData(percent, system))
			for ua_k, system, percent in pool_flattened
		)
		if not cls.__pool_or_init():
			raise UserAgentError(
				f"Extremely weird problem: config file is loaded ({cls.pool_file}), "
				f"but the {cls} is still empty."
			)

	def _save(cls):
		pool_flattened = cls.flat_pool()
		with open(cls.pool_file, 'wt', encoding='utf-8') as f:
			return _json.dump(pool_flattened, f)

	@staticmethod
	def __flat_pool_sort(pool_flattened: _i[UserAgent]):
		return sorted(pool_flattened, key=UserAgent.sort_key)

	def __flat_pool(cls, load=False, sort=True):
		pool = cls.__pool_or_load() if load else cls.__pool_or_init()
		pool_flattened = [
			UserAgent(ua_k, ua_data.system, ua_data.percent)
			for ua_k, ua_data in pool.items()
		]
		if not sort:
			return pool_flattened
		return cls.__flat_pool_sort(pool_flattened)

	def flat_pool(cls, sort=True):
		return cls.__flat_pool(load=True, sort=sort)

	@property
	def random(cls):
		"""Get a random user-agent"""
		cls.__pool_or_load()
		ua = _rnd_choices(cls.__flat_uas, cum_weights=cls.__flat_cumul_weights, k=1)[0]
		return UserAgent(ua, cls.__map_to_sys[ua], cls.__map_to_percent[ua])


class UserAgentPool(_StaticDataClass, metaclass=_UserAgentPoolMeta):
	"""
	Container class that provides user-agent strings on demand, with their
	distribution matching real browser market share.

	The list of most common browsers is loaded from ``user_agent_pool.json`` file.


	Usage::

		# get random user-agent (the pool is automatically loaded from config):
		UserAgentPool.random

		# Add new entry to the pool:
		UserAgentPool[
			"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:94.0) Gecko/20100101 Firefox/94.0"
		] = (4.8, "Firefox 94.0 Win10")

		# ... or many items at once:
		UserAgentPool.update( items: Iterable[(str, (percent: int, system: str))] )

		# remove entry:
		UserAgentPool.pop(user_agent_string)

		# save updated pool to the JSON config file:
		UserAgentPool._save()
	"""
	pass


if __name__ == '__main__':
	_test_n = 20
	_best_n = UserAgentPool.flat_pool()[:_test_n]
	print(f"{_test_n} most used agents:")
	for _ua, _sys, _pc in _best_n:
		print(f'{_pc:.2f}%\t{_ua}')

	print(f"\n{_test_n} most used systems:")
	for _ua, _sys, _pc in _best_n:
		print(f'{_pc:.2f}%\t{_sys}')

	from timeit import timeit as _timeit
	from collections import defaultdict as _defaultdict

	_test_n = 100000
	_random_stats: _d[UserAgent, int] = _defaultdict(int)

	def __query_random():
		_random_stats[UserAgentPool.random] += 1

	_time_took = _timeit(__query_random, number=_test_n)
	_random_total = sum(_random_stats.values())
	_results = sorted(
		[
			(100.0 * _hits / _random_total, _ua) for _ua, _hits in _random_stats.items()
		],
		reverse=True
	)
	_res_str = ',\n\t'.join(
		f'{_pc:.2f}%:\t({_ua.user_agent!r}, {_ua.system!r}, {_ua.percent})'
		for _pc, _ua in _results
	)

	print(
		f"\nReal destribution ({_test_n} random requests):\n\t{_res_str}"
		f"\n\ntook: {_time_took} sec ({_test_n} random requests)."
	)
