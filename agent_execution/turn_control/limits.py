"""Immutable per-turn limits; recovery cannot expand a saved execution envelope."""


def bounded(name, value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be {low}..{high}')
    return value


def execution_limits(saved, requested_iterations, requested_provider_retries, requested_eval_retries):
    """Return limits for this run, preferring an existing checkpoint verbatim."""
    from . import iteration_limit
    saved = saved or {}
    iterations = iteration_limit(None if 'iteration_limit' in saved else requested_iterations,
                                 saved.get('iteration_limit'))
    provider = bounded('max_retries', saved.get('provider_retry_limit', requested_provider_retries), 0, 256)
    evaluation = bounded('max_eval_retries', saved.get('eval_retry_limit', requested_eval_retries), 0, 256)
    return iterations, provider, evaluation
