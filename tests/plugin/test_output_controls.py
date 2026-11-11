"""Output-only safe text policy; requests and music grammar stay strict."""
import unittest
from plugin.wire import decode, reply, truncate


class OutputControls(unittest.TestCase):
    def test_crlf_cr_tab_and_forbidden_controls(self):
        self.assertEqual(truncate('Привет\tмир\r\nДа\rНет\x01\x7f', 77),
                         'Привет мир\nДа\nНет')

    def test_each_forbidden_c0_and_del(self):
        for code in [*range(1, 9), 11, 12, *range(14, 32), 127]:
            with self.subTest(code=code):
                self.assertEqual(reply('00000001', 'o', 'a' + chr(code) + 'б'),
                                 'R1|00000001|o|aб')

    def test_exact_budget_after_normalization(self):
        source = 'Я' * 34 + '\t\r\n\r😀\x01\x7f'
        expected = 'Я' * 34 + ' \n\n😀'
        self.assertEqual(len(expected.encode()), 75)
        self.assertEqual(reply('00000001', 'o', source + 'аб'),
                         'R1|00000001|o|' + expected[:-1] + '…')
        exact = 'Я' * 35 + '\t\r\n\r😀\x01\x7f'
        result = reply('00000001', 'o', exact)
        self.assertEqual(result, 'R1|00000001|o|' + 'Я' * 35 + ' \n\n😀')
        self.assertEqual(len(result.encode()), 91)

    def test_invalid_scalars_fail_closed_even_past_limit(self):
        for bad in ('\x00', '\ufffd', '\ud800', '\udfff'):
            for prefix in ('', 'я' * 100):
                with self.subTest(bad=repr(bad), prefix=len(prefix)):
                    with self.assertRaises((ValueError, UnicodeError)):
                        reply('00000001', 'o', prefix + bad)

    def test_normalized_empty_fails_closed(self):
        with self.assertRaises(ValueError):
            reply('00000001', 'o', '\x01\x7f')

    def test_m_grammar_never_normalized(self):
        for text in ('1,0,35\r', '1,0,35\t', '1,0,\x0135', '1,0,035', '1,0,101'):
            with self.subTest(text=repr(text)):
                with self.assertRaises(ValueError): reply('00000001', 'm', text)
        self.assertEqual(reply('00000001', 'm', '1,0,35'), 'R1|00000001|m|1,0,35')

    def test_p1_input_never_normalized(self):
        for char in ('\t', '\r', '\x01', '\x7f'):
            with self.subTest(char=repr(char)):
                with self.assertRaises(ValueError): decode('P1|00000001|c|a' + char + 'b')
