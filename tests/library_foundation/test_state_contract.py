import unittest
from tests.library_foundation import test_foundation as base
from evolvmem import library_foundation as api


class StateContractTests(unittest.TestCase):
    def test_sql_authority_states_can_be_read_without_guessing_aliases(self):
        try:
            library = api.LibraryDescriptor(base.LIBRARY, 'project', 'postgres', 'pg.synthetic',
                7, 11, 'pg_active', 'ready', 'postgres', project_id=base.PROJECT)
        except ValueError:
            self.fail('the versioned control SQL state pg_active/ready is not accepted')
        self.assertEqual('pg_active', library.authority_state)


if __name__ == '__main__':
    unittest.main()
