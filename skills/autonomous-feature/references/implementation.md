# Implementation phase

Implement the accepted plan incrementally:

- follow repository conventions;
- add or update tests alongside behavior;
- keep public interfaces backward compatible unless the accepted specification says otherwise;
- include migration and rollback support when applicable;
- update user-facing and operator documentation affected by the change;
- do not create commits unless the user explicitly requested them.

Record phase progress when useful:

```bash
controller.py set-phase --phase implementing
```

Boundaries: preserve unrelated user changes; never weaken authorization, validation, tests, or
static checks to make the workflow pass.
