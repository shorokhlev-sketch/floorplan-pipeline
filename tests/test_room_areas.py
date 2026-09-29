# -*- coding: utf-8 -*-
"""Юнит-тесты для tools/plan-room-areas.py.

Запуск: uv run pytest tests/test_room_areas.py
"""
import importlib.util
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.normpath(os.path.join(TESTS_DIR, '..', 'tools'))
# на случай, если модуль когда-нибудь переименуют без дефисов и его можно
# будет импортировать напрямую — держим tools/ в sys.path
sys.path.insert(0, TOOLS_DIR)

MODULE_PATH = os.path.join(TOOLS_DIR, 'plan-room-areas.py')
_spec = importlib.util.spec_from_file_location('plan_room_areas', MODULE_PATH)
pra = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pra)


def make_synthetic_unit():
    """400x300 пол, вертикальная стена 10px на x=195..205, дверной проём 80px
    (y=100..180) закрыт двумя коробками 4x10, колонна 60x60 в левой комнате.

    Аналитика:
      левая комната  = 195*300 - 60*60 = 58500-3600 = 54900 px^2 = 5.49 m^2
      правая комната = 205..400 * 300  = 195*300     = 58500   px^2 = 5.85 m^2
    """
    return {
        'unit': 'TEST',
        'frame': '0:0',
        'w': 400.0,
        'h': 300.0,
        'floor': [[0, 0], [400, 0], [400, 300], [0, 300]],
        'balconies': [],
        'ink': [
            [[195, 0], [205, 0], [205, 100], [195, 100]],     # верхний простенок
            [[195, 180], [205, 180], [205, 300], [195, 300]],  # нижний простенок
            [[50, 50], [110, 50], [110, 110], [50, 110]],      # колонна 60x60
        ],
        'white': [],
        'jambs': [
            {'id': 'j-top', 'poly': [[195, 96], [205, 96], [205, 100], [195, 100]]},
            {'id': 'j-bottom', 'poly': [[195, 180], [205, 180], [205, 184], [195, 184]]},
        ],
        'labels': [],
        'living': 11.34,
        'title': 'TEST unit',
    }


class TestFillPolygon(unittest.TestCase):
    def test_rectangle_area_exact(self):
        w, h = 50, 40
        mask = bytearray(w * h)
        pra.fill_polygon(mask, w, h, [[10, 10], [40, 10], [40, 30], [10, 30]], 1)
        self.assertEqual(sum(mask), 30 * 20)  # 30 по x, 20 по y


class TestDoorClosures(unittest.TestCase):
    def test_pair_within_range_closes(self):
        jambs = [
            {'id': 'a', 'poly': [[195, 96], [205, 96], [205, 100], [195, 100]]},
            {'id': 'b', 'poly': [[195, 180], [205, 180], [205, 184], [195, 184]]},
        ]
        closures = pra.find_door_closures(jambs)
        self.assertEqual(len(closures), 1)
        gx0, gy0, gx1, gy1 = closures[0]
        self.assertAlmostEqual(gx0, 195)
        self.assertAlmostEqual(gx1, 205)
        self.assertAlmostEqual(gy0, 100)
        self.assertAlmostEqual(gy1, 180)

    def test_gap_too_wide_not_paired(self):
        # зазор 200px — вне диапазона 40..130, дверь не закрывается
        jambs = [
            {'id': 'a', 'poly': [[195, 0], [205, 0], [205, 4], [195, 4]]},
            {'id': 'b', 'poly': [[195, 204], [205, 204], [205, 208], [195, 208]]},
        ]
        closures = pra.find_door_closures(jambs)
        self.assertEqual(len(closures), 0)


class TestSyntheticUnit(unittest.TestCase):
    def setUp(self):
        self.data = make_synthetic_unit()
        self.result = pra.analyze(self.data)

    def test_two_rooms_found(self):
        self.assertEqual(len(self.result['rooms']), 2)

    def test_one_door_closed(self):
        self.assertEqual(self.result['n_doors_closed'], 1)

    def test_areas_match_analytic(self):
        areas = sorted(r['area_m2'] for r in self.result['rooms'])
        self.assertAlmostEqual(areas[0], 5.49, delta=0.2)
        self.assertAlmostEqual(areas[1], 5.85, delta=0.2)

    def test_sum_close_to_living(self):
        self.assertAlmostEqual(self.result['sum_m2'], 11.34, delta=0.3)
        self.assertIsNotNone(self.result['diff_pct'])
        self.assertLess(abs(self.result['diff_pct']), 5)

    def test_rooms_sorted_desc_and_labeled(self):
        cells = [r['cells'] for r in self.result['rooms']]
        self.assertEqual(cells, sorted(cells, reverse=True))
        for r in self.result['rooms']:
            self.assertIn('m²', r['label'])
            self.assertEqual(r['label_collision'], False)

    def test_no_spurious_warnings(self):
        # диапазон living подобран так, что расхождение и доля комнаты в норме
        self.assertEqual(self.result['warnings'], [])


class TestNoDoorMergesRooms(unittest.TestCase):
    def test_without_jambs_rooms_merge_into_one(self):
        data = make_synthetic_unit()
        data['jambs'] = []  # без коробок проём не закрывается — комнаты сливаются
        result = pra.analyze(data)
        self.assertEqual(len(result['rooms']), 1)
        self.assertEqual(result['n_doors_closed'], 0)


if __name__ == '__main__':
    unittest.main()
