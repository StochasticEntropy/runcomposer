"""Reference plugins shipped with runcomposer.

These are ordinary plugins: they register through the same entry-point groups
(`runcomposer.sources`, `runcomposer.runners`) any third-party plugin uses,
and the core never imports them (guarded by a test).
"""
