# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.
# Modifications Copyright OpenSearch Contributors. See
# GitHub history for details.
# Licensed to Elasticsearch B.V. under one or more contributor
# license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright
# ownership. Elasticsearch B.V. licenses this file to you under
# the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# 	http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=protected-access

import random
import shutil
import tempfile
from unittest import TestCase

import numpy as np

from osbenchmark import exceptions
from osbenchmark.utils import io
from osbenchmark.utils.dataset import Context, HDF5DataSet
from osbenchmark.utils.parse import ConfigurationError
from osbenchmark.workload import params, workload, loader
from osbenchmark.workload.params import VectorDataSetPartitionParamSource, VectorSearchPartitionParamSource, \
    BulkVectorsFromDataSetParamSource
from tests.utils.dataset_helper import create_data_set, create_attributes_data_set, create_parent_data_set
from tests.utils.dataset_test import DEFAULT_NUM_VECTORS


class StaticBulkReader:
    def __init__(self, index_name, type_name, bulks):
        self.index_name = index_name
        self.type_name = type_name
        self.bulks = iter(bulks)

    def __enter__(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        batch = []
        bulk = next(self.bulks)
        batch.append((len(bulk), bulk))
        return self.index_name, self.type_name, batch

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class SliceTests(TestCase):
    def test_slice_with_source_larger_than_slice(self):
        source = params.Slice(io.StringAsFileSource, 2, 5)
        data = [
            '{"key": "value1"}',
            '{"key": "value2"}',
            '{"key": "value3"}',
            '{"key": "value4"}',
            '{"key": "value5"}',
            '{"key": "value6"}',
            '{"key": "value7"}',
            '{"key": "value8"}',
            '{"key": "value9"}',
            '{"key": "value10"}'
        ]

        source.open(data, "r", 5)
        # lines are returned as a list so we have to wrap our data once more
        self.assertEqual([data[2:7]], list(source))
        source.close()

    def test_slice_with_slice_larger_than_source(self):
        source = params.Slice(io.StringAsFileSource, 0, 5)
        data = [
            '{"key": "value1"}',
            '{"key": "value2"}',
            '{"key": "value3"}',
        ]

        source.open(data, "r", 5)
        # lines are returned as a list so we have to wrap our data once more
        self.assertEqual([data], list(source))
        source.close()


class ConflictingIdsBuilderTests(TestCase):
    def test_no_id_conflicts(self):
        self.assertIsNone(params.build_conflicting_ids(None, 100, 0))
        self.assertIsNone(params.build_conflicting_ids(params.IndexIdConflict.NoConflicts, 100, 0))

    def test_sequential_conflicts(self):
        self.assertEqual(
            [
                '0000000000',
                '0000000001',
                '0000000002',
                '0000000003',
                '0000000004',
                '0000000005',
                '0000000006',
                '0000000007',
                '0000000008',
                '0000000009',
                '0000000010'
            ],
            params.build_conflicting_ids(params.IndexIdConflict.SequentialConflicts, 11, 0)
        )

        self.assertEqual(
            [
                '0000000005',
                '0000000006',
                '0000000007',
                '0000000008',
                '0000000009',
                '0000000010',
                '0000000011',
                '0000000012',
                '0000000013',
                '0000000014',
                '0000000015'
            ],
            params.build_conflicting_ids(params.IndexIdConflict.SequentialConflicts, 11, 5)
        )

    def test_random_conflicts(self):
        predictable_shuffle = list.reverse

        self.assertEqual(
            [
                '0000000002', '0000000001', '0000000000'
            ],
            params.build_conflicting_ids(params.IndexIdConflict.RandomConflicts, 3, 0, shuffle=predictable_shuffle)
        )

        self.assertEqual(
            [
                '0000000007', '0000000006', '0000000005'
            ],
            params.build_conflicting_ids(params.IndexIdConflict.RandomConflicts, 3, 5, shuffle=predictable_shuffle)
        )


class ActionMetaDataTests(TestCase):
    def test_generate_action_meta_data_without_id_conflicts(self):
        self.assertEqual(("index", '{"index": {"_index": "test_index", "_type": "test_type"}}\n'),
                         next(params.GenerateActionMetaData("test_index", "test_type")))

    def test_generate_action_meta_data_create(self):
        self.assertEqual(("create", '{"create": {"_index": "test_index"}}\n'),
                         next(params.GenerateActionMetaData("test_index", None, use_create=True)))

    def test_generate_action_meta_data_create_with_conflicts(self):
        with self.assertRaises(exceptions.BenchmarkError) as ctx:
            params.GenerateActionMetaData("test_index", None, conflicting_ids=[100, 200, 300, 400], use_create=True)
        self.assertEqual("Index mode '_create' cannot be used with conflicting ids",
                         ctx.exception.args[0])

    def test_generate_action_meta_data_typeless(self):
        self.assertEqual(("index", '{"index": {"_index": "test_index"}}\n'),
                         next(params.GenerateActionMetaData("test_index", type_name=None)))

    def test_generate_action_meta_data_with_id_conflicts(self):
        def idx(id):
            return "index", '{"index": {"_index": "test_index", "_type": "test_type", "_id": "%s"}}\n' % id

        def conflict(action, id):
            return action, '{"%s": {"_index": "test_index", "_type": "test_type", "_id": "%s"}}\n' % (action, id)

        pseudo_random_conflicts = iter([
            # if this value is <= our chosen threshold of 0.25 (see conflict_probability) we produce a conflict.
            0.2,
            0.25,
            0.2,
            # no conflict
            0.3,
            # conflict again
            0.0
        ])

        chosen_index_of_conflicting_ids = iter([
            # the "random" index of the id in the array `conflicting_ids` that will produce a conflict
            1,
            3,
            2,
            0])

        conflict_action = random.choice(["index", "update"])

        generator = params.GenerateActionMetaData("test_index", "test_type",
                                                  conflicting_ids=[100, 200, 300, 400],
                                                  conflict_probability=25,
                                                  on_conflict=conflict_action,
                                                  rand=lambda: next(pseudo_random_conflicts),
                                                  randint=lambda x, y: next(chosen_index_of_conflicting_ids))

        # first one is always *not* drawn from a random index
        self.assertEqual(idx("100"), next(generator))
        # now we start using random ids, i.e. look in the first line of the pseudo-random sequence
        self.assertEqual(conflict(conflict_action, "200"), next(generator))
        self.assertEqual(conflict(conflict_action, "400"), next(generator))
        self.assertEqual(conflict(conflict_action, "300"), next(generator))
        # no conflict -> we draw the next sequential one, which is 200
        self.assertEqual(idx("200"), next(generator))
        # and we're back to random
        self.assertEqual(conflict(conflict_action, "100"), next(generator))

    def test_generate_action_meta_data_with_id_conflicts_and_recency_bias(self):
        def idx(type_name, id):
            if type_name:
                return "index", '{"index": {"_index": "test_index", "_type": "%s", "_id": "%s"}}\n' % (type_name, id)
            else:
                return "index", '{"index": {"_index": "test_index", "_id": "%s"}}\n' % id

        def conflict(action, type_name, id):
            if type_name:
                return action, '{"%s": {"_index": "test_index", "_type": "%s", "_id": "%s"}}\n' % (action, type_name, id)
            else:
                return action, '{"%s": {"_index": "test_index", "_id": "%s"}}\n' % (action, id)

        pseudo_random_conflicts = iter([
            # if this value is <= our chosen threshold of 0.25 (see conflict_probability) we produce a conflict.
            0.2,
            0.25,
            0.2,
            # no conflict
            0.3,
            0.4,
            0.35,
            # conflict again
            0.0,
            0.2,
            0.15
        ])

        # we use this value as `idx_range` in the calculation: idx = round((self.id_up_to - 1) * (1 - idx_range))
        pseudo_exponential_distribution = iter([
            # id_up_to = 1 -> idx = 0
            0.013375248172714948,
            # id_up_to = 1 -> idx = 0
            0.042495604491024914,
            # id_up_to = 1 -> idx = 0
            0.005491072642023834,
            # no conflict: id_up_to = 2
            # no conflict: id_up_to = 3
            # no conflict: id_up_to = 4
            # id_up_to = 4 -> idx = round((4 - 1) * (1 - 0.028557879547255083)) = 3
            0.028557879547255083,
            # id_up_to = 4 -> idx = round((4 - 1) * (1 - 0.209771474243926352)) = 2
            0.209771474243926352
        ])

        conflict_action = random.choice(["index", "update"])
        type_name = random.choice([None, "test_type"])

        generator = params.GenerateActionMetaData("test_index", type_name=type_name,
                                                  conflicting_ids=[100, 200, 300, 400, 500, 600],
                                                  conflict_probability=25,
                                                  # heavily biased towards recent ids
                                                  recency=1.0,
                                                  on_conflict=conflict_action,
                                                  rand=lambda: next(pseudo_random_conflicts),
                                                  # we don't use this one here because recency is > 0.
                                                  # randint=lambda x, y: next(chosen_index_of_conflicting_ids),
                                                  randexp=lambda lmbda: next(pseudo_exponential_distribution)
                                                  )

        # first one is always *not* drawn from a random index
        self.assertEqual(idx(type_name, "100"), next(generator))
        # now we start using random ids
        self.assertEqual(conflict(conflict_action, type_name, "100"), next(generator))
        self.assertEqual(conflict(conflict_action, type_name, "100"), next(generator))
        self.assertEqual(conflict(conflict_action, type_name, "100"), next(generator))
        # no conflict
        self.assertEqual(idx(type_name, "200"), next(generator))
        self.assertEqual(idx(type_name, "300"), next(generator))
        self.assertEqual(idx(type_name, "400"), next(generator))
        # conflict
        self.assertEqual(conflict(conflict_action, type_name, "400"), next(generator))
        self.assertEqual(conflict(conflict_action, type_name, "300"), next(generator))

    def test_generate_action_meta_data_with_id_and_zero_conflict_probability(self):
        def idx(id):
            return "index", '{"index": {"_index": "test_index", "_type": "test_type", "_id": "%s"}}\n' % id

        test_ids = [100, 200, 300, 400]

        generator = params.GenerateActionMetaData("test_index", "test_type",
                                                  conflicting_ids=test_ids,
                                                  conflict_probability=0)

        self.assertListEqual([idx(id) for id in test_ids], list(generator))


class IndexDataReaderTests(TestCase):
    def test_read_bulk_larger_than_number_of_docs(self):
        data = [
            b'{"key": "value1"}\n',
            b'{"key": "value2"}\n',
            b'{"key": "value3"}\n',
            b'{"key": "value4"}\n',
            b'{"key": "value5"}\n'
        ]
        bulk_size = 50

        source = params.Slice(io.StringAsFileSource, 0, len(data))
        am_handler = params.GenerateActionMetaData("test_index", "test_type")

        reader = params.MetadataIndexDataReader(data,
                                                batch_size=bulk_size,
                                                bulk_size=bulk_size,
                                                file_source=source,
                                                action_metadata=am_handler,
                                                index_name="test_index",
                                                type_name="test_type")

        expected_bulk_sizes = [len(data)]
        # lines should include meta-data
        expected_line_sizes = [len(data) * 2]
        self.assert_bulks_sized(reader, expected_bulk_sizes, expected_line_sizes)

    def test_read_bulk_with_offset(self):
        data = [
            b'{"key": "value1"}\n',
            b'{"key": "value2"}\n',
            b'{"key": "value3"}\n',
            b'{"key": "value4"}\n',
            b'{"key": "value5"}\n'
        ]
        bulk_size = 50

        source = params.Slice(io.StringAsFileSource, 3, len(data))
        am_handler = params.GenerateActionMetaData("test_index", "test_type")

        reader = params.MetadataIndexDataReader(data,
                                                batch_size=bulk_size,
                                                bulk_size=bulk_size,
                                                file_source=source,
                                                action_metadata=am_handler,
                                                index_name="test_index",
                                                type_name="test_type")

        expected_bulk_sizes = [(len(data) - 3)]
        # lines should include meta-data
        expected_line_sizes = [(len(data) - 3) * 2]
        self.assert_bulks_sized(reader, expected_bulk_sizes, expected_line_sizes)

    def test_read_bulk_smaller_than_number_of_docs(self):
        data = [
            b'{"key": "value1"}\n',
            b'{"key": "value2"}\n',
            b'{"key": "value3"}\n',
            b'{"key": "value4"}\n',
            b'{"key": "value5"}\n',
            b'{"key": "value6"}\n',
            b'{"key": "value7"}\n',
        ]
        bulk_size = 3

        source = params.Slice(io.StringAsFileSource, 0, len(data))
        am_handler = params.GenerateActionMetaData("test_index", "test_type")

        reader = params.MetadataIndexDataReader(data,
                                                batch_size=bulk_size,
                                                bulk_size=bulk_size,
                                                file_source=source,
                                                action_metadata=am_handler,
                                                index_name="test_index",
                                                type_name="test_type")

        expected_bulk_sizes = [3, 3, 1]
        # lines should include meta-data
        expected_line_sizes = [6, 6, 2]
        self.assert_bulks_sized(reader, expected_bulk_sizes, expected_line_sizes)

    def test_read_bulk_smaller_than_number_of_docs_and_multiple_clients(self):
        data = [
            b'{"key": "value1"}\n',
            b'{"key": "value2"}\n',
            b'{"key": "value3"}\n',
            b'{"key": "value4"}\n',
            b'{"key": "value5"}\n',
            b'{"key": "value6"}\n',
            b'{"key": "value7"}\n',
        ]
        bulk_size = 3

        # only 5 documents to index for this client
        source = params.Slice(io.StringAsFileSource, 0, 5)
        am_handler = params.GenerateActionMetaData("test_index", "test_type")

        reader = params.MetadataIndexDataReader(data,
                                                batch_size=bulk_size,
                                                bulk_size=bulk_size,
                                                file_source=source,
                                                action_metadata=am_handler,
                                                index_name="test_index",
                                                type_name="test_type")

        expected_bulk_sizes = [3, 2]
        # lines should include meta-data
        expected_line_sizes = [6, 4]
        self.assert_bulks_sized(reader, expected_bulk_sizes, expected_line_sizes)

    def test_read_bulks_and_assume_metadata_line_in_source_file(self):
        data = [
            b'{"index": {"_index": "test_index", "_type": "test_type"}\n',
            b'{"key": "value1"}\n',
            b'{"index": {"_index": "test_index", "_type": "test_type"}\n',
            b'{"key": "value2"}\n',
            b'{"index": {"_index": "test_index", "_type": "test_type"}\n',
            b'{"key": "value3"}\n',
            b'{"index": {"_index": "test_index", "_type": "test_type"}\n',
            b'{"key": "value4"}\n',
            b'{"index": {"_index": "test_index", "_type": "test_type"}\n',
            b'{"key": "value5"}\n',
            b'{"index": {"_index": "test_index", "_type": "test_type"}\n',
            b'{"key": "value6"}\n',
            b'{"index": {"_index": "test_index", "_type": "test_type"}\n',
            b'{"key": "value7"}\n'
        ]
        bulk_size = 3

        source = params.Slice(io.StringAsFileSource, 0, len(data))

        reader = params.SourceOnlyIndexDataReader(data,
                                                  batch_size=bulk_size,
                                                  bulk_size=bulk_size,
                                                  file_source=source,
                                                  index_name="test_index",
                                                  type_name="test_type")

        expected_bulk_sizes = [3, 3, 1]
        # lines should include meta-data
        expected_line_sizes = [6, 6, 2]
        self.assert_bulks_sized(reader, expected_bulk_sizes, expected_line_sizes)

    def test_read_bulk_with_id_conflicts(self):
        pseudo_random_conflicts = iter([
            # if this value is <= our chosen threshold of 0.25 (see conflict_probability) we produce a conflict.
            0.2,
            0.25,
            0.2,
            # no conflict
            0.3
        ])

        chosen_index_of_conflicting_ids = iter([
            # the "random" index of the id in the array `conflicting_ids` that will produce a conflict
            1,
            3,
            2])

        data = [
            b'{"key": "value1"}\n',
            b'{"key": "value2"}\n',
            b'{"key": "value3"}\n',
            b'{"key": "value4"}\n',
            b'{"key": "value5"}\n'
        ]
        bulk_size = 2

        source = params.Slice(io.StringAsFileSource, 0, len(data))
        am_handler = params.GenerateActionMetaData("test_index", "test_type",
                                                   conflicting_ids=[100, 200, 300, 400],
                                                   conflict_probability=25,
                                                   on_conflict="update",
                                                   rand=lambda: next(pseudo_random_conflicts),
                                                   randint=lambda x, y: next(chosen_index_of_conflicting_ids))

        reader = params.MetadataIndexDataReader(data,
                                                batch_size=bulk_size,
                                                bulk_size=bulk_size,
                                                file_source=source,
                                                action_metadata=am_handler,
                                                index_name="test_index",
                                                type_name="test_type")

        # consume all bulks
        bulks = []
        with reader:
            for _, _, batch in reader:
                for bulk_size, bulk in batch:
                    bulks.append(bulk)

        self.assertEqual([
            b'{"index": {"_index": "test_index", "_type": "test_type", "_id": "100"}}\n' +
            b'{"key": "value1"}\n' +
            b'{"update": {"_index": "test_index", "_type": "test_type", "_id": "200"}}\n' +
            b'{"doc":{"key": "value2"}}\n',
            b'{"update": {"_index": "test_index", "_type": "test_type", "_id": "400"}}\n' +
            b'{"doc":{"key": "value3"}}\n' +
            b'{"update": {"_index": "test_index", "_type": "test_type", "_id": "300"}}\n' +
            b'{"doc":{"key": "value4"}}\n',
            b'{"index": {"_index": "test_index", "_type": "test_type", "_id": "200"}}\n' +
            b'{"key": "value5"}\n'
        ], bulks)

    def test_read_bulk_with_external_id_and_zero_conflict_probability(self):
        data = [
            b'{"key": "value1"}\n',
            b'{"key": "value2"}\n',
            b'{"key": "value3"}\n',
            b'{"key": "value4"}\n'
        ]
        bulk_size = 2

        source = params.Slice(io.StringAsFileSource, 0, len(data))
        am_handler = params.GenerateActionMetaData("test_index", "test_type",
                                                   conflicting_ids=[100, 200, 300, 400],
                                                   conflict_probability=0)

        reader = params.MetadataIndexDataReader(data,
                                                batch_size=bulk_size,
                                                bulk_size=bulk_size,
                                                file_source=source,
                                                action_metadata=am_handler,
                                                index_name="test_index",
                                                type_name="test_type")

        # consume all bulks
        bulks = []
        with reader:
            for _, _, batch in reader:
                for bulk_size, bulk in batch:
                    bulks.append(bulk)

        self.assertEqual([
            b'{"index": {"_index": "test_index", "_type": "test_type", "_id": "100"}}\n' +
            b'{"key": "value1"}\n' +
            b'{"index": {"_index": "test_index", "_type": "test_type", "_id": "200"}}\n' +
            b'{"key": "value2"}\n',

            b'{"index": {"_index": "test_index", "_type": "test_type", "_id": "300"}}\n' +
            b'{"key": "value3"}\n' +
            b'{"index": {"_index": "test_index", "_type": "test_type", "_id": "400"}}\n' +
            b'{"key": "value4"}\n'
        ], bulks)

    def assert_bulks_sized(self, reader, expected_bulk_sizes, expected_line_sizes):
        self.assertEqual(len(expected_bulk_sizes), len(expected_line_sizes), "Bulk sizes and line sizes must be equal")
        with reader:
            bulk_index = 0
            for _, _, batch in reader:
                for bulk_size, bulk in batch:
                    self.assertEqual(expected_bulk_sizes[bulk_index], bulk_size, msg="bulk size")
                    self.assertEqual(expected_line_sizes[bulk_index], bulk.count(b"\n"))
                    bulk_index += 1
            self.assertEqual(len(expected_bulk_sizes), bulk_index, "Not all bulk sizes have been checked")


class InvocationGeneratorTests(TestCase):
    class TestIndexReader:
        def __init__(self, data):
            self.enter_count = 0
            self.exit_count = 0
            self.data = data

        def __enter__(self):
            self.enter_count += 1
            return self

        def __iter__(self):
            return iter(self.data)

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.exit_count += 1
            return False

    class TestIndex:
        def __init__(self, name, types):
            self.name = name
            self.types = types

    class TestType:
        def __init__(self, number_of_documents, includes_action_and_meta_data=False):
            self.number_of_documents = number_of_documents
            self.includes_action_and_meta_data = includes_action_and_meta_data

    def idx(self, *args, **kwargs):
        return InvocationGeneratorTests.TestIndex(*args, **kwargs)

    def t(self, *args, **kwargs):
        return InvocationGeneratorTests.TestType(*args, **kwargs)

    def test_iterator_chaining_respects_context_manager(self):
        i0 = InvocationGeneratorTests.TestIndexReader([1, 2, 3])
        i1 = InvocationGeneratorTests.TestIndexReader([4, 5, 6])

        self.assertEqual([1, 2, 3, 4, 5, 6], list(params.chain(i0, i1)))
        self.assertEqual(1, i0.enter_count)
        self.assertEqual(1, i0.exit_count)
        self.assertEqual(1, i1.enter_count)
        self.assertEqual(1, i1.exit_count)

    def test_calculate_bounds(self):
        num_docs = 1000
        clients = 1
        self.assertEqual((0, 1000, 1000), params.bounds(num_docs, 0, 0, clients, includes_action_and_meta_data=False))
        self.assertEqual((0, 1000, 2000), params.bounds(num_docs, 0, 0, clients, includes_action_and_meta_data=True))

        num_docs = 1000
        clients = 2
        self.assertEqual((0, 500, 500), params.bounds(num_docs, 0, 0, clients, includes_action_and_meta_data=False))
        self.assertEqual((500, 500, 500), params.bounds(num_docs, 1, 1, clients, includes_action_and_meta_data=False))

        num_docs = 800
        clients = 4
        self.assertEqual((0, 200, 400), params.bounds(num_docs, 0, 0, clients, includes_action_and_meta_data=True))
        self.assertEqual((400, 200, 400), params.bounds(num_docs, 1, 1, clients, includes_action_and_meta_data=True))
        self.assertEqual((800, 200, 400), params.bounds(num_docs, 2, 2, clients, includes_action_and_meta_data=True))
        self.assertEqual((1200, 200, 400), params.bounds(num_docs, 3, 3, clients, includes_action_and_meta_data=True))

        num_docs = 2000
        clients = 8
        self.assertEqual((0, 250, 250), params.bounds(num_docs, 0, 0, clients, includes_action_and_meta_data=False))
        self.assertEqual((250, 250, 250), params.bounds(num_docs, 1, 1, clients, includes_action_and_meta_data=False))
        self.assertEqual((500, 250, 250), params.bounds(num_docs, 2, 2, clients, includes_action_and_meta_data=False))
        self.assertEqual((750, 250, 250), params.bounds(num_docs, 3, 3, clients, includes_action_and_meta_data=False))
        self.assertEqual((1000, 250, 250), params.bounds(num_docs, 4, 4, clients, includes_action_and_meta_data=False))
        self.assertEqual((1250, 250, 250), params.bounds(num_docs, 5, 5, clients, includes_action_and_meta_data=False))
        self.assertEqual((1500, 250, 250), params.bounds(num_docs, 6, 6, clients, includes_action_and_meta_data=False))
        self.assertEqual((1750, 250, 250), params.bounds(num_docs, 7, 7, clients, includes_action_and_meta_data=False))

    def test_calculate_non_multiple_bounds_16_clients(self):
        # in this test case, each client would need to read 1333.3333 lines. Instead we let most clients read 1333
        # lines and every third client, one line more (1334).
        num_docs = 16000
        clients = 12
        self.assertEqual((0, 1333, 1333), params.bounds(num_docs, 0, 0, clients, includes_action_and_meta_data=False))
        self.assertEqual((1333, 1334, 1334), params.bounds(num_docs, 1, 1, clients, includes_action_and_meta_data=False))
        self.assertEqual((2667, 1333, 1333), params.bounds(num_docs, 2, 2, clients, includes_action_and_meta_data=False))
        self.assertEqual((4000, 1333, 1333), params.bounds(num_docs, 3, 3, clients, includes_action_and_meta_data=False))
        self.assertEqual((5333, 1334, 1334), params.bounds(num_docs, 4, 4, clients, includes_action_and_meta_data=False))
        self.assertEqual((6667, 1333, 1333), params.bounds(num_docs, 5, 5, clients, includes_action_and_meta_data=False))
        self.assertEqual((8000, 1333, 1333), params.bounds(num_docs, 6, 6, clients, includes_action_and_meta_data=False))
        self.assertEqual((9333, 1334, 1334), params.bounds(num_docs, 7, 7, clients, includes_action_and_meta_data=False))
        self.assertEqual((10667, 1333, 1333), params.bounds(num_docs, 8, 8, clients, includes_action_and_meta_data=False))
        self.assertEqual((12000, 1333, 1333), params.bounds(num_docs, 9, 9, clients, includes_action_and_meta_data=False))
        self.assertEqual((13333, 1334, 1334), params.bounds(num_docs, 10, 10, clients, includes_action_and_meta_data=False))
        self.assertEqual((14667, 1333, 1333), params.bounds(num_docs, 11, 11, clients, includes_action_and_meta_data=False))

    def test_calculate_non_multiple_bounds_6_clients(self):
        # With 3500 docs and 6 clients, every client needs to read 583.33 docs. We have two lines per doc, which makes it
        # 2 * 583.333 docs = 1166.6666 lines per client. We let them read 1166 and 1168 lines respectively (583 and 584 docs).
        num_docs = 3500
        clients = 6
        self.assertEqual((0, 583, 1166), params.bounds(num_docs, 0, 0, clients, includes_action_and_meta_data=True))
        self.assertEqual((1166, 584, 1168), params.bounds(num_docs, 1, 1, clients, includes_action_and_meta_data=True))
        self.assertEqual((2334, 583, 1166), params.bounds(num_docs, 2, 2, clients, includes_action_and_meta_data=True))
        self.assertEqual((3500, 583, 1166), params.bounds(num_docs, 3, 3, clients, includes_action_and_meta_data=True))
        self.assertEqual((4666, 584, 1168), params.bounds(num_docs, 4, 4, clients, includes_action_and_meta_data=True))
        self.assertEqual((5834, 583, 1166), params.bounds(num_docs, 5, 5, clients, includes_action_and_meta_data=True))

    def test_calculate_bounds_for_multiple_clients_per_worker(self):
        num_docs = 2000
        clients = 8
        # four clients per worker, each reads 250 lines
        self.assertEqual((0, 1000, 1000), params.bounds(num_docs, 0, 3, clients, includes_action_and_meta_data=False))
        self.assertEqual((1000, 1000, 1000), params.bounds(num_docs, 4, 7, clients, includes_action_and_meta_data=False))

        # four clients per worker, each reads 500 lines (includes action and metadata)
        self.assertEqual((0, 1000, 2000), params.bounds(num_docs, 0, 3, clients, includes_action_and_meta_data=True))
        self.assertEqual((2000, 1000, 2000), params.bounds(num_docs, 4, 7, clients, includes_action_and_meta_data=True))

    def test_calculate_number_of_bulks(self):
        docs1 = self.docs(1)
        docs2 = self.docs(2)

        self.assertEqual(1, self.number_of_bulks([self.corpus("a", [docs1])], 0, 0, 1, 1))
        self.assertEqual(1, self.number_of_bulks([self.corpus("a", [docs1])], 0, 0, 1, 2))
        self.assertEqual(20, self.number_of_bulks(
            [self.corpus("a", [docs2, docs2, docs2, docs2, docs1]),
             self.corpus("b", [docs2, docs2, docs2, docs2, docs2, docs1])], 0, 0, 1, 1))
        self.assertEqual(11, self.number_of_bulks(
            [self.corpus("a", [docs2, docs2, docs2, docs2, docs1]),
             self.corpus("b", [docs2, docs2, docs2, docs2, docs2, docs1])], 0, 0, 1, 2))
        self.assertEqual(11, self.number_of_bulks(
            [self.corpus("a", [docs2, docs2, docs2, docs2, docs1]),
             self.corpus("b", [docs2, docs2, docs2, docs2, docs2, docs1])], 0, 0, 1, 3))
        self.assertEqual(11, self.number_of_bulks(
            [self.corpus("a", [docs2, docs2, docs2, docs2, docs1]),
             self.corpus("b", [docs2, docs2, docs2, docs2, docs2, docs1])], 0, 0, 1, 100))

        self.assertEqual(2, self.number_of_bulks([self.corpus("a", [self.docs(800)])], 0, 0, 3, 250))
        self.assertEqual(1, self.number_of_bulks([self.corpus("a", [self.docs(800)])], 0, 0, 3, 267))
        self.assertEqual(1, self.number_of_bulks([self.corpus("a", [self.docs(80)])], 0, 0, 3, 267))
        # this looks odd at first but we are prioritizing number of clients above bulk size
        self.assertEqual(1, self.number_of_bulks([self.corpus("a", [self.docs(80)])], 1, 1, 3, 267))
        self.assertEqual(1, self.number_of_bulks([self.corpus("a", [self.docs(80)])], 2, 2, 3, 267))

    @staticmethod
    def corpus(name, docs):
        return workload.DocumentCorpus(name, documents=docs)

    @staticmethod
    def docs(num_docs):
        return workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK, number_of_documents=num_docs)

    @staticmethod
    def number_of_bulks(corpora, first_partition_index, last_partition_index, total_partitions, bulk_size):
        return params.number_of_bulks(corpora, first_partition_index, last_partition_index, total_partitions, bulk_size)

    def test_build_conflicting_ids(self):
        self.assertIsNone(params.build_conflicting_ids(params.IndexIdConflict.NoConflicts, 3, 0))
        self.assertEqual(["0000000000", "0000000001", "0000000002"],
                         params.build_conflicting_ids(params.IndexIdConflict.SequentialConflicts, 3, 0))
        # we cannot tell anything specific about the contents...
        self.assertEqual(3, len(params.build_conflicting_ids(params.IndexIdConflict.RandomConflicts, 3, 0)))


# pylint: disable=too-many-public-methods
class BulkIndexParamSourceTests(TestCase):
    def test_create_without_params(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )])

        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test", corpora=[corpus]), params={})

        self.assertEqual("Mandatory parameter 'bulk-size' is missing", ctx.exception.args[0])

    def test_create_without_corpora_definition(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test"), params={})

        self.assertEqual("There is no document corpus definition for workload unit-test. "
                         "You must add at least one before making bulk requests to OpenSearch.", ctx.exception.args[0])

    def test_create_with_non_numeric_bulk_size(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )])

        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test", corpora=[corpus]), params={
                "bulk-size": "Three"
            })

        self.assertEqual("'bulk-size' must be numeric", ctx.exception.args[0])

    def test_create_with_negative_bulk_size(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )])

        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test", corpora=[corpus]), params={
                "bulk-size": -5
            })

        self.assertEqual("'bulk-size' must be positive but was -5", ctx.exception.args[0])

    def test_create_with_fraction_smaller_batch_size(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )])

        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test", corpora=[corpus]), params={
                "bulk-size": 5,
                "batch-size": 3
            })

        self.assertEqual("'batch-size' must be greater than or equal to 'bulk-size'", ctx.exception.args[0])

    def test_create_with_fraction_larger_batch_size(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )])

        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test", corpora=[corpus]), params={
                "bulk-size": 5,
                "batch-size": 8
            })

        self.assertEqual("'batch-size' must be a multiple of 'bulk-size'", ctx.exception.args[0])

    def test_create_with_metadata_in_source_file_but_conflicts(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            document_archive="docs.json.bz2",
                            document_file="docs.json",
                            number_of_documents=10,
                            includes_action_and_meta_data=True)
        ])

        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test", corpora=[corpus]), params={
                "conflicts": "random"
            })

        self.assertEqual("Cannot generate id conflicts [random] as [docs.json.bz2] in document corpus [default] already contains "
                         "an action and meta-data line.", ctx.exception.args[0])

    def test_create_with_unknown_id_conflicts(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test"), params={
                "conflicts": "crazy"
            })

        self.assertEqual("Unknown 'conflicts' setting [crazy]", ctx.exception.args[0])

    def test_create_with_unknown_on_conflict_setting(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test"), params={
                "conflicts": "sequential",
                "on-conflict": "delete"
            })

        self.assertEqual("Unknown 'on-conflict' setting [delete]", ctx.exception.args[0])

    def test_create_with_conflicts_and_data_streams(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test"), params={
                "data-streams": ["test-data-stream-1", "test-data-stream-2"],
                "conflicts": "sequential"
            })

        self.assertEqual("'conflicts' cannot be used with 'data-streams'", ctx.exception.args[0])

    def test_create_with_ingest_percentage_too_low(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )])

        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test", corpora=[corpus]), params={
                "bulk-size": 5000,
                "ingest-percentage": 0.0
            })

        self.assertEqual("'ingest-percentage' must be in the range (0.0, 100.0] but was 0.0", ctx.exception.args[0])

    def test_create_with_ingest_percentage_too_high(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )])

        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test", corpora=[corpus]), params={
                "bulk-size": 5000,
                "ingest-percentage": 100.1
            })

        self.assertEqual("'ingest-percentage' must be in the range (0.0, 100.0] but was 100.1", ctx.exception.args[0])

    def test_create_with_ingest_percentage_not_numeric(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )])

        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test", corpora=[corpus]), params={
                "bulk-size": 5000,
                "ingest-percentage": "100 percent"
            })

        self.assertEqual("'ingest-percentage' must be numeric", ctx.exception.args[0])

    def test_create_valid_param_source(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )])

        self.assertIsNotNone(params.BulkIndexParamSource(workload.Workload(name="unit-test", corpora=[corpus]), params={
            "conflicts": "random",
            "bulk-size": 5000,
            "batch-size": 20000,
            "ingest-percentage": 20.5,
            "pipeline": "test-pipeline"
        }))

    def test_passes_all_corpora_by_default(self):
        corpora = [
            workload.DocumentCorpus(name="default", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=10,
                                target_index="test-idx",
                                target_type="test-type"
                                )
            ]),
            workload.DocumentCorpus(name="special", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=100,
                                target_index="test-idx2",
                                target_type="type"
                                )
            ]),
        ]

        source = params.BulkIndexParamSource(
            workload=workload.Workload(name="unit-test", corpora=corpora),
            params={
                "conflicts": "random",
                "bulk-size": 5000,
                "batch-size": 20000,
                "pipeline": "test-pipeline"
            })

        partition = source.partition(0, 1)
        self.assertEqual(partition.corpora, corpora)

    def test_filters_corpora(self):
        corpora = [
            workload.DocumentCorpus(name="default", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=10,
                                target_index="test-idx",
                                target_type="test-type"
                                )
            ]),
            workload.DocumentCorpus(name="special", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=100,
                                target_index="test-idx2",
                                target_type="type"
                                )
            ]),
        ]

        source = params.BulkIndexParamSource(
            workload=workload.Workload(name="unit-test", corpora=corpora),
            params={
                "corpora": ["special"],
                "conflicts": "random",
                "bulk-size": 5000,
                "batch-size": 20000,
                "pipeline": "test-pipeline"
            })

        partition = source.partition(0, 1)
        self.assertEqual(partition.corpora, [corpora[1]])

    def test_filters_corpora_by_data_stream(self):
        corpora = [
            workload.DocumentCorpus(name="default", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=10,
                                target_data_stream="test-data-stream-1"
                                )
            ]),
            workload.DocumentCorpus(name="special", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=100,
                                target_index="test-idx2",
                                target_type="type"
                                )
            ]),
            workload.DocumentCorpus(name="special-2", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=10,
                                target_data_stream="test-data-stream-2"
                                )
            ])
        ]

        source = params.BulkIndexParamSource(
            workload=workload.Workload(name="unit-test", corpora=corpora),
            params={
                "data-streams": ["test-data-stream-1", "test-data-stream-2"],
                "bulk-size": 5000,
                "batch-size": 20000,
                "pipeline": "test-pipeline"
            })

        partition = source.partition(0, 1)
        self.assertEqual(partition.corpora, [corpora[0], corpora[2]])

    def test_raises_exception_if_no_corpus_matches(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )])

        with self.assertRaises(exceptions.BenchmarkAssertionError) as ctx:
            params.BulkIndexParamSource(
                workload=workload.Workload(name="unit-test", corpora=[corpus]),
                params={
                    "corpora": "does_not_exist",
                    "conflicts": "random",
                    "bulk-size": 5000,
                    "batch-size": 20000,
                    "pipeline": "test-pipeline"
                })

        self.assertEqual("The provided corpus ['does_not_exist'] does not match any of the corpora ['default'].", ctx.exception.args[0])

    def test_ingests_all_documents_by_default(self):
        corpora = [
            workload.DocumentCorpus(name="default", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=300000,
                                target_index="test-idx",
                                target_type="test-type"
                                )
            ]),
            workload.DocumentCorpus(name="special", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=700000,
                                target_index="test-idx2",
                                target_type="type"
                                )
            ]),
        ]

        source = params.BulkIndexParamSource(
            workload=workload.Workload(name="unit-test", corpora=corpora),
            params={
                "bulk-size": 10000
            })

        partition = source.partition(0, 1)
        partition._init_internal_params()
        # # no ingest-percentage specified, should issue all one hundred bulk requests
        self.assertEqual(100, partition.total_bulks)

    def test_restricts_number_of_bulks_if_required(self):
        def create_unit_test_reader(*args):
            return StaticBulkReader("idx", "doc", bulks=[
                ['{"location" : [-0.1485188, 51.5250666]}'],
                ['{"location" : [-0.1479949, 51.5252071]}'],
                ['{"location" : [-0.1458559, 51.5289059]}'],
                ['{"location" : [-0.1498551, 51.5282564]}'],
                ['{"location" : [-0.1487043, 51.5254843]}'],
                ['{"location" : [-0.1533367, 51.5261779]}'],
                ['{"location" : [-0.1543018, 51.5262398]}'],
                ['{"location" : [-0.1522118, 51.5266564]}'],
                ['{"location" : [-0.1529092, 51.5263360]}'],
                ['{"location" : [-0.1537008, 51.5265365]}'],
            ])

        def schedule(param_source):
            while True:
                try:
                    yield param_source.params()
                except StopIteration:
                    return

        corpora = [
            workload.DocumentCorpus(name="default", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=300000,
                                target_index="test-idx",
                                target_type="test-type"
                                )
            ]),
            workload.DocumentCorpus(name="special", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=700000,
                                target_index="test-idx2",
                                target_type="type"
                                )
            ]),
        ]

        source = params.BulkIndexParamSource(
            workload=workload.Workload(name="unit-test", corpora=corpora),
            params={
                "bulk-size": 10000,
                "ingest-percentage": 2.5,
                "__create_reader": create_unit_test_reader
            })

        partition = source.partition(0, 1)
        partition._init_internal_params()
        # should issue three bulks of size 10.000
        self.assertEqual(3, partition.total_bulks)
        self.assertEqual(3, len(list(schedule(partition))))

    def test_create_with_conflict_probability_zero(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )])

        params.BulkIndexParamSource(workload=workload.Workload(name="unit-test", corpora=[corpus]), params={
            "bulk-size": 5000,
            "conflicts": "sequential",
            "conflict-probability": 0
        })

    def test_create_with_conflict_probability_too_low(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test"), params={
                "bulk-size": 5000,
                "conflicts": "sequential",
                "conflict-probability": -0.1
            })

        self.assertEqual("'conflict-probability' must be in the range [0.0, 100.0] but was -0.1", ctx.exception.args[0])

    def test_create_with_conflict_probability_too_high(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test"), params={
                "bulk-size": 5000,
                "conflicts": "sequential",
                "conflict-probability": 100.1
            })

        self.assertEqual("'conflict-probability' must be in the range [0.0, 100.0] but was 100.1", ctx.exception.args[0])

    def test_create_with_conflict_probability_not_numeric(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.BulkIndexParamSource(workload=workload.Workload(name="unit-test"), params={
                "bulk-size": 5000,
                "conflicts": "sequential",
                "conflict-probability": "100 percent"
            })

        self.assertEqual("'conflict-probability' must be numeric", ctx.exception.args[0])


class BulkDataGeneratorTests(TestCase):

    @classmethod
    def create_test_reader(cls, batches):
        def inner_create_test_reader(docs, *args):
            return StaticBulkReader(docs.target_index, docs.target_type, batches)

        return inner_create_test_reader

    def test_generate_two_bulks(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=10,
                            target_index="test-idx",
                            target_type="test-type"
                            )
        ])

        bulks = params.bulk_data_based(num_clients=1, start_client_index=0, end_client_index=0, corpora=[corpus],
                                       batch_size=5, bulk_size=5,
                                       id_conflicts=params.IndexIdConflict.NoConflicts, conflict_probability=None, on_conflict=None,
                                       recency=None, pipeline=None,
                                       original_params={
                                           "my-custom-parameter": "foo",
                                           "my-custom-parameter-2": True
                                       }, create_reader=BulkDataGeneratorTests.
                                       create_test_reader([["1", "2", "3", "4", "5"], ["6", "7", "8"]]))
        all_bulks = list(bulks)
        self.assertEqual(2, len(all_bulks))
        self.assertEqual({
            "action-metadata-present": True,
            "body": ["1", "2", "3", "4", "5"],
            "bulk-size": 5,
            "unit": "docs",
            "index": "test-idx",
            "type": "test-type",
            "my-custom-parameter": "foo",
            "my-custom-parameter-2": True
        }, all_bulks[0])

        self.assertEqual({
            "action-metadata-present": True,
            "body": ["6", "7", "8"],
            "bulk-size": 3,
            "unit": "docs",
            "index": "test-idx",
            "type": "test-type",
            "my-custom-parameter": "foo",
            "my-custom-parameter-2": True
        }, all_bulks[1])

    def test_generate_bulks_from_multiple_corpora(self):
        corpora = [
            workload.DocumentCorpus(name="default", documents=[
                        workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                        number_of_documents=5,
                                        target_index="logs-2018-01",
                                        target_type="docs"
                                        ),
                        workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                        number_of_documents=5,
                                        target_index="logs-2018-02",
                                        target_type="docs"
                                        ),

                    ]),
            workload.DocumentCorpus(name="special", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                                number_of_documents=5,
                                target_index="logs-2017-01",
                                target_type="docs"
                                )
            ])

            ]

        bulks = params.bulk_data_based(num_clients=1, start_client_index=0, end_client_index=0, corpora=corpora,
                                       batch_size=5, bulk_size=5,
                                       id_conflicts=params.IndexIdConflict.NoConflicts, conflict_probability=None, on_conflict=None,
                                       recency=None, pipeline=None,
                                       original_params={
                                           "my-custom-parameter": "foo",
                                           "my-custom-parameter-2": True
                                       }, create_reader=BulkDataGeneratorTests.
                                       create_test_reader([["1", "2", "3", "4", "5"]]))
        all_bulks = list(bulks)
        self.assertEqual(3, len(all_bulks))
        self.assertEqual({
            "action-metadata-present": True,
            "body": ["1", "2", "3", "4", "5"],
            "bulk-size": 5,
            "unit": "docs",
            "index": "logs-2018-01",
            "type": "docs",
            "my-custom-parameter": "foo",
            "my-custom-parameter-2": True
        }, all_bulks[0])

        self.assertEqual({
            "action-metadata-present": True,
            "body": ["1", "2", "3", "4", "5"],
            "bulk-size": 5,
            "unit": "docs",
            "index": "logs-2018-02",
            "type": "docs",
            "my-custom-parameter": "foo",
            "my-custom-parameter-2": True
        }, all_bulks[1])

        self.assertEqual({
            "action-metadata-present": True,
            "body": ["1", "2", "3", "4", "5"],
            "bulk-size": 5,
            "unit": "docs",
            "index": "logs-2017-01",
            "type": "docs",
            "my-custom-parameter": "foo",
            "my-custom-parameter-2": True
        }, all_bulks[2])

    def test_internal_params_take_precedence(self):
        corpus = workload.DocumentCorpus(name="default", documents=[
            workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_BULK,
                            number_of_documents=3,
                            target_index="test-idx",
                            target_type="test-type"
                            )
        ])

        bulks = params.bulk_data_based(num_clients=1, start_client_index=0, end_client_index=0, corpora=[corpus],
                                       batch_size=3, bulk_size=3, id_conflicts=params.IndexIdConflict.NoConflicts,
                                       conflict_probability=None, on_conflict=None,
                                       recency=None, pipeline=None,
                                       original_params={
                                           "body": "foo",
                                           "custom-param": "bar"
                                       }, create_reader=BulkDataGeneratorTests.
                                       create_test_reader([["1", "2", "3"]]))
        all_bulks = list(bulks)
        self.assertEqual(1, len(all_bulks))
        # body must not contain 'foo'!
        self.assertEqual({
            "action-metadata-present": True,
            "body": ["1", "2", "3"],
            "bulk-size": 3,
            "unit": "docs",
            "index": "test-idx",
            "type": "test-type",
            "custom-param": "bar"
        }, all_bulks[0])


class ParamsRegistrationTests(TestCase):
    @staticmethod
    def param_source_legacy_function(indices, params):
        return {
            "key": params["parameter"]
        }

    @staticmethod
    def param_source_function(workload, params, **kwargs):
        return {
            "key": params["parameter"]
        }

    class ParamSourceLegacyClass:
        def __init__(self, indices=None, params=None):
            self._indices = indices
            self._params = params

        def partition(self, partition_index, total_partitions):
            return self

        def size(self):
            return 1

        def params(self):
            return {
                "class-key": self._params["parameter"]
            }

    class ParamSourceClass:
        def __init__(self, workload=None, params=None, **kwargs):
            self._workload = workload
            self._params = params

        def partition(self, partition_index, total_partitions):
            return self

        def size(self):
            return 1

        def params(self):
            return {
                "class-key": self._params["parameter"]
            }

        def __str__(self):
            return "test param source"

    def test_can_register_legacy_function_as_param_source(self):
        source_name = "legacy-params-test-function-param-source"

        params.register_param_source_for_name(source_name, ParamsRegistrationTests.param_source_legacy_function)
        source = params.param_source_for_name(source_name, workload.Workload(name="unit-test"), {"parameter": 42})
        self.assertEqual({"key": 42}, source.params())

        params._unregister_param_source_for_name(source_name)

    def test_can_register_function_as_param_source(self):
        source_name = "params-test-function-param-source"

        params.register_param_source_for_name(source_name, ParamsRegistrationTests.param_source_function)
        source = params.param_source_for_name(source_name, workload.Workload(name="unit-test"), {"parameter": 42})
        self.assertEqual({"key": 42}, source.params())

        params._unregister_param_source_for_name(source_name)

    def test_can_register_legacy_class_as_param_source(self):
        source_name = "legacy-params-test-class-param-source"

        params.register_param_source_for_name(source_name, ParamsRegistrationTests.ParamSourceLegacyClass)
        source = params.param_source_for_name(source_name, workload.Workload(name="unit-test"), {"parameter": 42})
        self.assertEqual({"class-key": 42}, source.params())

        params._unregister_param_source_for_name(source_name)

    def test_can_register_class_as_param_source(self):
        source_name = "params-test-class-param-source"

        params.register_param_source_for_name(source_name, ParamsRegistrationTests.ParamSourceClass)
        source = params.param_source_for_name(source_name, workload.Workload(name="unit-test"), {"parameter": 42})
        self.assertEqual({"class-key": 42}, source.params())

        params._unregister_param_source_for_name(source_name)

    def test_cannot_register_an_instance_as_param_source(self):
        source_name = "params-test-class-param-source"
        # we create an instance, instead of passing the class
        with self.assertRaisesRegex(exceptions.BenchmarkAssertionError,
                                    "Parameter source \\[test param source\\] must be either a function or a class\\."):
            params.register_param_source_for_name(source_name, ParamsRegistrationTests.ParamSourceClass())

class StandardValueSourceRegistrationTests(TestCase):
    def get_mock_standard_value_source(self, gte, lte):
        return lambda : {"gte":gte, "lte":lte}

    def test_register_standard_value_source(self):
        # Test the sequence: register standard value source -> generate saved standard values
        # -> retrieve those values or generate new values from source
        op_name = "op-1"
        field_name_1 = "field-1"
        field_name_2 = "field-2"
        n = 100

        gte_field_1 = 0
        lte_field_1 = 1
        gte_field_2 = 2
        lte_field_2 = 3

        params._clear_standard_values()

        params.register_standard_value_source(op_name, field_name_1, self.get_mock_standard_value_source(gte_field_1, lte_field_1))

        self.assertEqual(params.get_standard_value_source(op_name, field_name_1)(), {"gte":gte_field_1, "lte":lte_field_1})

        with self.assertRaises(exceptions.SystemSetupError) as ctx:
            _ = params.get_standard_value_source(op_name, field_name_2)
            self.assertEqual(
                "Could not find standard value source for operation {}, field {}! Make sure this is registered in workload.py"
                .format(op_name, field_name_2), ctx.exception.args[0])

        with self.assertRaises(exceptions.SystemSetupError) as ctx:
            _ = params.get_standard_value(op_name, field_name_1, 0)
            self.assertEqual("No standard values generated for operation {}, field {}".format(op_name, field_name_1), ctx.exception.args[0])

        params.generate_standard_values_if_absent(op_name, field_name_1, n)
        self.assertEqual(params.get_standard_value(op_name, field_name_1, 0), {"gte":gte_field_1, "lte":lte_field_1})

        # check that running generate_standard_values_if_absent on the same inputs does nothing
        # we can do this by telling it to generate 2*n, but it won't because values are already present
        params.generate_standard_values_if_absent(op_name, field_name_1, 2*n)
        with self.assertRaises(exceptions.SystemSetupError) as ctx:
            _ = params.get_standard_value(op_name, field_name_1, n + 1)
            self.assertEqual(
                "Standard value index {} out of range for operation {}, field name {} ({} values total)"
                .format(n+1, op_name, field_name_1, n), ctx.exception.args[0])

        with self.assertRaises(exceptions.SystemSetupError) as ctx:
            params.generate_standard_values_if_absent(op_name, field_name_2, n)
            self.assertEqual(
                "Cannot generate standard values for operation {}, field {}. Standard value source is missing"
                .format(op_name, field_name_2), ctx.exception.args[0])

        params.register_standard_value_source(op_name, field_name_2, self.get_mock_standard_value_source(gte_field_2, lte_field_2))
        self.assertEqual(params.get_standard_value_source(op_name, field_name_2)(), {"gte":gte_field_2, "lte":lte_field_2})
        self.assertEqual(params.get_standard_value_source(op_name, field_name_1)(), {"gte":gte_field_1, "lte":lte_field_1})

        params._clear_standard_values()

class QueryRandomizationInfoRegistrationTests(TestCase):
    def check_result_equality(self, result, expected):
        self.assertEqual(result.query_name, expected.query_name)
        self.assertEqual(result.parameter_name_options_list, expected.parameter_name_options_list)
        self.assertEqual(result.optional_parameters, expected.optional_parameters)


    def test_register_query_randomization_info(self):
        params._clear_query_randomization_infos()

        op_name = "op-1"
        query_name = "geo_bounding_box"
        value_name_options_list = [["top_left"], ["lower_right"]]
        optional_values = []
        params.register_query_randomization_info(op_name, query_name, value_name_options_list, optional_values)

        query_randomization_info = params.get_query_randomization_info(op_name)
        expected = loader.QueryRandomizerWorkloadProcessor.QueryRandomizationInfo("geo_bounding_box", [["top_left"], ["lower_right"]], [])
        self.check_result_equality(query_randomization_info, expected)

        # Should get the default one for an op that has nothing registered
        query_randomization_info = params.get_query_randomization_info("unrecognized-op")
        expected = loader.QueryRandomizerWorkloadProcessor.DEFAULT_QUERY_RANDOMIZATION_INFO
        self.check_result_equality(query_randomization_info, expected)

        params._clear_query_randomization_infos()

class SleepParamSourceTests(TestCase):
    def test_missing_duration_parameter(self):
        with self.assertRaisesRegex(exceptions.InvalidSyntax, "parameter 'duration' is mandatory for sleep operation"):
            params.SleepParamSource(workload.Workload(name="unit-test"), params={})

    def test_duration_parameter_wrong_type(self):
        with self.assertRaisesRegex(exceptions.InvalidSyntax,
                                    "parameter 'duration' for sleep operation must be a number"):
            params.SleepParamSource(workload.Workload(name="unit-test"), params={"duration": "this is a string"})

    def test_duration_parameter_negative_number(self):
        with self.assertRaisesRegex(exceptions.InvalidSyntax,
                                    "parameter 'duration' must be non-negative but was -1.0"):
            params.SleepParamSource(workload.Workload(name="unit-test"), params={"duration": -1.0})

    def test_param_source_passes_all_parameters(self):
        p = params.SleepParamSource(workload.Workload(name="unit-test"), params={"duration": 3.4, "additional": True})
        self.assertDictEqual({"duration": 3.4, "additional": True}, p.params())


class CreateIndexParamSourceTests(TestCase):
    def test_create_index_inline_with_body(self):
        source = params.CreateIndexParamSource(workload.Workload(name="unit-test"), params={
            "index": "test",
            "body": {
                "settings": {
                    "index.number_of_replicas": 0
                },
                "mappings": {
                    "doc": {
                        "properties": {
                            "name": {
                                "type": "keyword",
                            }
                        }
                    }
                }
            }
        })

        p = source.params()
        self.assertEqual(1, len(p["indices"]))
        index, body = p["indices"][0]
        self.assertEqual("test", index)
        self.assertTrue(len(body) > 0)
        self.assertEqual({}, p["request-params"])

    def test_create_index_inline_without_body(self):
        source = params.CreateIndexParamSource(workload.Workload(name="unit-test"), params={
            "index": "test",
            "request-params": {
                "wait_for_active_shards": True
            }
        })

        p = source.params()
        self.assertEqual(1, len(p["indices"]))
        index, body = p["indices"][0]
        self.assertEqual("test", index)
        self.assertIsNone(body)
        self.assertDictEqual({
            "wait_for_active_shards": True
        }, p["request-params"])

    def test_create_index_from_workload_with_settings(self):
        index1 = workload.Index(name="index1", types=["type1"])
        index2 = workload.Index(name="index2", types=["type1"], body={
            "settings": {
                "index.number_of_replicas": 0,
                "index.number_of_shards": 3
            },
            "mappings": {
                "type1": {
                    "properties": {
                        "name": {
                            "type": "keyword",
                        }
                    }
                }
            }
        })

        source = params.CreateIndexParamSource(workload.Workload(name="unit-test", indices=[index1, index2]), params={
            "settings": {
                "index.number_of_replicas": 1
            }
        })

        p = source.params()
        self.assertEqual(2, len(p["indices"]))

        index, body = p["indices"][0]
        self.assertEqual("index1", index)
        # index did not specify any body
        self.assertDictEqual({
            "settings": {
                "index.number_of_replicas": 1
            }
        }, body)

        index, body = p["indices"][1]
        self.assertEqual("index2", index)
        # index specified a body + we need to merge settings
        self.assertDictEqual({
            "settings": {
                # we have properly merged (overridden) an existing setting
                "index.number_of_replicas": 1,
                # and we have preserved one that was specified in the original index body
                "index.number_of_shards": 3
            },
            "mappings": {
                "type1": {
                    "properties": {
                        "name": {
                            "type": "keyword",
                        }
                    }
                }
            }
        }, body)

    def test_create_index_from_workload_without_settings(self):
        index1 = workload.Index(name="index1", types=["type1"])
        index2 = workload.Index(name="index2", types=["type1"], body={
            "settings": {
                "index.number_of_replicas": 0,
                "index.number_of_shards": 3
            },
            "mappings": {
                "type1": {
                    "properties": {
                        "name": {
                            "type": "keyword",
                        }
                    }
                }
            }
        })

        source = params.CreateIndexParamSource(workload.Workload(name="unit-test", indices=[index1, index2]), params={})

        p = source.params()
        self.assertEqual(2, len(p["indices"]))

        index, body = p["indices"][0]
        self.assertEqual("index1", index)
        # index did not specify any body
        self.assertDictEqual({}, body)

        index, body = p["indices"][1]
        self.assertEqual("index2", index)
        # index specified a body
        self.assertDictEqual({
            "settings": {
                "index.number_of_replicas": 0,
                "index.number_of_shards": 3
            },
            "mappings": {
                "type1": {
                    "properties": {
                        "name": {
                            "type": "keyword",
                        }
                    }
                }
            }
        }, body)

    def test_filter_index(self):
        index1 = workload.Index(name="index1", types=["type1"])
        index2 = workload.Index(name="index2", types=["type1"])
        index3 = workload.Index(name="index3", types=["type1"])

        source = params.CreateIndexParamSource(workload.Workload(name="unit-test", indices=[index1, index2, index3]), params={
            "index": "index2"
        })

        p = source.params()
        self.assertEqual(1, len(p["indices"]))

        index, _ = p["indices"][0]
        self.assertEqual("index2", index)

    def test_create_index_with_default_codec(self):
        source = params.CreateIndexParamSource(workload.Workload(name="unit-test"), params={
            "index": "test",
            "body": {
                "settings": {
                    "index.number_of_replicas": 0,
                    "index.codec": "default"
                },
                "mappings": {
                    "doc": {
                        "properties": {
                            "name": {
                                "type": "keyword",
                            }
                        }
                    }
                }
            }
        })

        p = source.params()
        self.assertEqual(1, len(p["indices"]))
        index, body = p["indices"][0]
        self.assertEqual("test", index)
        self.assertTrue(len(body) > 0)
        self.assertEqual({}, p["request-params"])
        self.assertEqual("default", body["settings"]["index.codec"])

    def test_create_index_with_best_compression_codec(self):
        source = params.CreateIndexParamSource(workload.Workload(name="unit-test"), params={
            "index": "test",
            "body": {
                "settings": {
                    "index.number_of_replicas": 0,
                    "index.codec": "best_compression"
                },
                "mappings": {
                    "doc": {
                        "properties": {
                            "name": {
                                "type": "keyword",
                            }
                        }
                    }
                }
            }
        })

        p = source.params()
        self.assertEqual(1, len(p["indices"]))
        index, body = p["indices"][0]
        self.assertEqual("test", index)
        self.assertTrue(len(body) > 0)
        self.assertEqual({}, p["request-params"])
        self.assertEqual("best_compression", body["settings"]["index.codec"])

    def test_create_index_with_zstd_codec(self):
        source = params.CreateIndexParamSource(workload.Workload(name="unit-test"), params={
            "index": "test",
            "body": {
                "settings": {
                    "index.number_of_replicas": 0,
                    "index.codec": "zstd"
                },
                "mappings": {
                    "doc": {
                        "properties": {
                            "name": {
                                "type": "keyword",
                            }
                        }
                    }
                }
            }
        })

        p = source.params()
        self.assertEqual(1, len(p["indices"]))
        index, body = p["indices"][0]
        self.assertEqual("test", index)
        self.assertTrue(len(body) > 0)
        self.assertEqual({}, p["request-params"])
        self.assertEqual("zstd", body["settings"]["index.codec"])

    def test_create_index_with_zstdnodict_codec(self):
        source = params.CreateIndexParamSource(workload.Workload(name="unit-test"), params={
            "index": "test",
            "body": {
                "settings": {
                    "index.number_of_replicas": 0,
                    "index.codec": "zstd_no_dict"
                },
                "mappings": {
                    "doc": {
                        "properties": {
                            "name": {
                                "type": "keyword",
                            }
                        }
                    }
                }
            }
        })

        p = source.params()
        self.assertEqual(1, len(p["indices"]))
        index, body = p["indices"][0]
        self.assertEqual("test", index)
        self.assertTrue(len(body) > 0)
        self.assertEqual({}, p["request-params"])
        self.assertEqual("zstd_no_dict", body["settings"]["index.codec"])

    def test_create_index_with_invalid_codec(self):
        with self.assertRaises(exceptions.InvalidSyntax) as context:
            params.CreateIndexParamSource(workload.Workload(name="unit-test"), params={
                "index": "test",
                "body": {
                    "settings": {
                        "index.number_of_replicas": 0,
                        "index.codec": "invalid_codec"
                    },
                    "mappings": {
                        "doc": {
                            "properties": {
                                "name": {
                                    "type": "keyword",
                                }
                            }
                        }
                    }
                }
            })

        self.assertEqual(str(context.exception),
                         "Please set the value properly for the create-index operation. Invalid index.codec value " +
                         "'invalid_codec'. Choose from available codecs: ['default', 'best_compression', 'zstd', 'zstd_no_dict', 'qat_deflate', 'qat_lz4']")

class CreateDataStreamParamSourceTests(TestCase):
    def test_create_data_stream(self):
        source = params.CreateDataStreamParamSource(workload.Workload(name="unit-test"), params={
            "data-stream": "test-data-stream"
        })
        p = source.params()
        self.assertEqual(1, len(p["data-streams"]))
        ds = p["data-streams"][0]
        self.assertEqual("test-data-stream", ds)
        self.assertEqual({}, p["request-params"])

    def test_create_data_stream_inline_without_body(self):
        source = params.CreateDataStreamParamSource(workload.Workload(name="unit-test"), params={
            "data-stream": "test-data-stream",
            "request-params": {
                "wait_for_active_shards": True
            }
        })

        p = source.params()
        self.assertEqual(1, len(p["data-streams"]))
        ds = p["data-streams"][0]
        self.assertEqual("test-data-stream", ds)
        self.assertDictEqual({
            "wait_for_active_shards": True
        }, p["request-params"])

    def test_filter_data_stream(self):
        source = params.CreateDataStreamParamSource(
            workload.Workload(name="unit-test", data_streams=[workload.DataStream(name="data-stream-1"),
                                                        workload.DataStream(name="data-stream-2"),
                                                        workload.DataStream(name="data-stream-3")]),
            params={"data-stream": "data-stream-2"})

        p = source.params()
        self.assertEqual(1, len(p["data-streams"]))

        ds = p["data-streams"][0]
        self.assertEqual("data-stream-2", ds)


class DeleteIndexParamSourceTests(TestCase):
    def test_delete_index_from_workload(self):
        source = params.DeleteIndexParamSource(workload.Workload(name="unit-test", indices=[
            workload.Index(name="index1"),
            workload.Index(name="index2"),
            workload.Index(name="index3")
        ]), params={})

        p = source.params()

        self.assertEqual(["index1", "index2", "index3"], p["indices"])
        self.assertDictEqual({}, p["request-params"])
        self.assertTrue(p["only-if-exists"])

    def test_filter_index_from_workload(self):
        source = params.DeleteIndexParamSource(workload.Workload(name="unit-test", indices=[
            workload.Index(name="index1"),
            workload.Index(name="index2"),
            workload.Index(name="index3")
        ]), params={"index": "index2", "only-if-exists": False, "request-params": {"allow_no_indices": True}})

        p = source.params()

        self.assertEqual(["index2"], p["indices"])
        self.assertDictEqual({"allow_no_indices": True}, p["request-params"])
        self.assertFalse(p["only-if-exists"])

    def test_delete_index_by_name(self):
        source = params.DeleteIndexParamSource(workload.Workload(name="unit-test"), params={"index": "index2"})

        p = source.params()

        self.assertEqual(["index2"], p["indices"])

    def test_delete_no_index(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.DeleteIndexParamSource(workload.Workload(name="unit-test"), params={})
        self.assertEqual("delete-index operation targets no index", ctx.exception.args[0])


class DeleteDataStreamParamSourceTests(TestCase):
    def test_delete_data_stream_from_workload(self):
        source = params.DeleteDataStreamParamSource(workload.Workload(name="unit-test", data_streams=[
            workload.DataStream(name="data-stream-1"),
            workload.DataStream(name="data-stream-2"),
            workload.DataStream(name="data-stream-3")
        ]), params={})

        p = source.params()

        self.assertEqual(["data-stream-1", "data-stream-2", "data-stream-3"], p["data-streams"])
        self.assertDictEqual({}, p["request-params"])
        self.assertTrue(p["only-if-exists"])

    def test_filter_data_stream_from_workload(self):
        source = params.DeleteDataStreamParamSource(workload.Workload(name="unit-test", data_streams=[
            workload.DataStream(name="data-stream-1"),
            workload.DataStream(name="data-stream-2"),
            workload.DataStream(name="data-stream-3")
        ]), params={"data-stream": "data-stream-2", "only-if-exists": False,
                    "request-params": {"allow_no_indices": True}})

        p = source.params()

        self.assertEqual(["data-stream-2"], p["data-streams"])
        self.assertDictEqual({"allow_no_indices": True}, p["request-params"])
        self.assertFalse(p["only-if-exists"])

    def test_delete_data_stream_by_name(self):
        source = params.DeleteDataStreamParamSource(workload.Workload(name="unit-test"),
                                                    params={"data-stream": "data-stream-2"})

        p = source.params()

        self.assertEqual(["data-stream-2"], p["data-streams"])

    def test_delete_no_data_stream(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.DeleteDataStreamParamSource(workload.Workload(name="unit-test"), params={})
        self.assertEqual("delete-data-stream operation targets no data stream", ctx.exception.args[0])


class CreateIndexTemplateParamSourceTests(TestCase):
    def test_create_index_template_inline(self):
        source = params.CreateIndexTemplateParamSource(workload=workload.Workload(name="unit-test"), params={
            "template": "test",
            "body": {
                "index_patterns": ["*"],
                "settings": {
                    "index.number_of_shards": 3
                },
                "mappings": {
                    "docs": {
                        "_source": {
                            "enabled": False
                        }
                    }
                }
            }
        })

        p = source.params()

        self.assertEqual(1, len(p["templates"]))
        self.assertDictEqual({}, p["request-params"])
        template, body = p["templates"][0]
        self.assertEqual("test", template)
        self.assertDictEqual({
            "index_patterns": ["*"],
            "settings": {
                "index.number_of_shards": 3
            },
            "mappings": {
                "docs": {
                    "_source": {
                        "enabled": False
                    }
                }
            }
        }, body)

    def test_create_index_template_from_workload(self):
        tpl = workload.IndexTemplate(name="default", pattern="*", content={
            "index_patterns": ["*"],
            "settings": {
                "index.number_of_shards": 3
            },
            "mappings": {
                "docs": {
                    "_source": {
                        "enabled": False
                    }
                }
            }
        })

        source = params.CreateIndexTemplateParamSource(workload=workload.Workload(name="unit-test", templates=[tpl]), params={
            "settings": {
                "index.number_of_replicas": 1
            }
        })

        p = source.params()

        self.assertEqual(1, len(p["templates"]))
        self.assertDictEqual({}, p["request-params"])
        template, body = p["templates"][0]
        self.assertEqual("default", template)
        self.assertDictEqual({
            "index_patterns": ["*"],
            "settings": {
                "index.number_of_shards": 3,
                "index.number_of_replicas": 1
            },
            "mappings": {
                "docs": {
                    "_source": {
                        "enabled": False
                    }
                }
            }
        }, body)


class DeleteIndexTemplateParamSourceTests(TestCase):
    def test_delete_index_template_by_name(self):
        source = params.DeleteIndexTemplateParamSource(workload.Workload(name="unit-test"), params={"template": "default"})

        p = source.params()

        self.assertEqual(1, len(p["templates"]))
        self.assertEqual(("default", False, None), p["templates"][0])
        self.assertTrue(p["only-if-exists"])
        self.assertDictEqual({}, p["request-params"])

    def test_delete_index_template_by_name_and_matching_indices(self):
        source = params.DeleteIndexTemplateParamSource(workload.Workload(name="unit-test"),
                                                       params={
                                                           "template": "default",
                                                           "delete-matching-indices": True,
                                                           "index-pattern": "logs-*"
                                                       })

        p = source.params()

        self.assertEqual(1, len(p["templates"]))
        self.assertEqual(("default", True, "logs-*"), p["templates"][0])
        self.assertTrue(p["only-if-exists"])
        self.assertDictEqual({}, p["request-params"])

    def test_delete_index_template_by_name_and_matching_indices_missing_index_pattern(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.DeleteIndexTemplateParamSource(workload.Workload(name="unit-test"),
                                                  params={
                                                      "template": "default",
                                                      "delete-matching-indices": True
                                                  })
        self.assertEqual("The property 'index-pattern' is required for delete-index-template if 'delete-matching-indices' is true.",
                         ctx.exception.args[0])

    def test_delete_index_template_from_workload(self):
        tpl1 = workload.IndexTemplate(name="metrics", pattern="metrics-*", delete_matching_indices=True, content={
            "index_patterns": ["metrics-*"],
            "settings": {},
            "mappings": {}
        })
        tpl2 = workload.IndexTemplate(name="logs", pattern="logs-*", delete_matching_indices=False, content={
            "index_patterns": ["logs-*"],
            "settings": {},
            "mappings": {}
        })

        source = params.DeleteIndexTemplateParamSource(workload.Workload(name="unit-test", templates=[tpl1, tpl2]), params={
            "request-params": {
                "master_timeout": 20
            },
            "only-if-exists": False
        })

        p = source.params()

        self.assertEqual(2, len(p["templates"]))
        self.assertEqual(("metrics", True, "metrics-*"), p["templates"][0])
        self.assertEqual(("logs", False, "logs-*"), p["templates"][1])
        self.assertFalse(p["only-if-exists"])
        self.assertDictEqual({"master_timeout": 20}, p["request-params"])


class CreateComposableTemplateParamSourceTests(TestCase):
    def test_create_index_template_inline(self):
        source = params.CreateComposableTemplateParamSource(workload=workload.Workload(name="unit-test"), params={
            "template": "test",
            "body": {
              "index_patterns": ["my*"],
              "template": {
                "settings" : {
                    "index.number_of_shards" : 3
                }
              },
              "composed_of": ["ct1", "ct2"]
            }
        })

        p = source.params()

        self.assertEqual(1, len(p["templates"]))
        self.assertDictEqual({}, p["request-params"])
        template, body = p["templates"][0]
        self.assertEqual("test", template)
        self.assertDictEqual({
              "index_patterns": ["my*"],
              "template": {
                "settings" : {
                    "index.number_of_shards" : 3
                }
              },
              "composed_of": ["ct1", "ct2"]
            }, body)

    def test_create_composable_index_template_from_workload(self):
        tpl = workload.IndexTemplate(name="default", pattern="*", content={
              "index_patterns": ["my*"],
              "template": {
                "settings" : {
                    "index.number_of_shards" : 3
                }
              },
              "composed_of": ["ct1", "ct2"]
            })

        source = params.CreateComposableTemplateParamSource(workload=workload.Workload(
            name="unit-test", composable_templates=[tpl]), params={
            "settings": {
                "index.number_of_replicas": 1
            }
        })

        p = source.params()

        self.assertEqual(1, len(p["templates"]))
        self.assertDictEqual({}, p["request-params"])
        template, body = p["templates"][0]
        self.assertEqual("default", template)
        self.assertDictEqual({
              "index_patterns": ["my*"],
              "template": {
                "settings" : {
                    "index.number_of_shards" : 3,
                    "index.number_of_replicas": 1
                }
              },
              "composed_of": ["ct1", "ct2"]
            }, body)

    def test_create_or_merge(self):
        content = params.CreateComposableTemplateParamSource._create_or_merge({"parent": {}}, ["parent", "child", "grandchild"],
                                                       {"name": "Mike"})
        assert content["parent"]["child"]["grandchild"]["name"] == "Mike"
        content = params.CreateComposableTemplateParamSource._create_or_merge({"parent": {"child": {}}}, ["parent", "child", "grandchild"],
                                                       {"name": "Mike"})
        assert content["parent"]["child"]["grandchild"]["name"] == "Mike"
        content = params.CreateComposableTemplateParamSource._create_or_merge({"parent": {"child": {"grandchild": {}}}},
                                                       ["parent", "child", "grandchild"], {"name": "Mike"})
        assert content["parent"]["child"]["grandchild"]["name"] == "Mike"
        content = params.CreateComposableTemplateParamSource._create_or_merge(
            {"parent": {"child": {"name": "Mary", "grandchild": {"name": "Dale", "age": 38}}}},
            ["parent", "child", "grandchild"], {"name": "Mike"})
        assert content["parent"]["child"]["name"] == "Mary"
        assert content["parent"]["child"]["grandchild"]["name"] == "Mike"
        assert content["parent"]["child"]["grandchild"]["age"] == 38
        content = params.CreateComposableTemplateParamSource._create_or_merge(
            {"parent": {
                "child": {"name": "Mary", "grandchild": {"name": {"first": "Dale", "last": "Smith"}, "age": 38}}}},
            ["parent", "child", "grandchild"], {"name": "Mike"})
        assert content["parent"]["child"]["grandchild"]["name"] == "Mike"
        assert content["parent"]["child"]["grandchild"]["age"] == 38
        content = params.CreateComposableTemplateParamSource._create_or_merge(
            {"parent": {
                "child": {"name": "Mary", "grandchild": {"name": {"first": "Dale", "last": "Smith"}, "age": 38}}}},
            ["parent", "child", "grandchild"], {"name": {"first": "Mike"}})
        assert content["parent"]["child"]["grandchild"]["name"]["first"] == "Mike"
        assert content["parent"]["child"]["grandchild"]["name"]["last"] == "Smith"

    def test_no_templates_specified(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.CreateComposableTemplateParamSource(
                workload=workload.Workload(name="unit-test"), params={
                    "settings": {
                        "index.number_of_shards": 1,
                        "index.number_of_replicas": 1
                    },
                    "operation-type": "create-composable-template"
                })
        self.assertEqual("Please set the properties 'template' and 'body' for the create-composable-template operation "
                         "or declare composable and/or component templates in the workload", ctx.exception.args[0])


class CreateComponentTemplateParamSourceTests(TestCase):
    def test_create_component_index_template_from_workload(self):
        tpl = workload.ComponentTemplate(name="default", content={
          "template": {
            "mappings": {
              "properties": {
                "@timestamp": {
                  "type": "date"
                }
              }
            }
          }
        })

        source = params.CreateComponentTemplateParamSource(
            workload=workload.Workload(name="unit-test", component_templates=[tpl]), params={
                "settings": {
                    "index.number_of_shards": 1,
                    "index.number_of_replicas": 1
                }
            })

        p = source.params()

        self.assertEqual(1, len(p["templates"]))
        self.assertDictEqual({}, p["request-params"])
        template, body = p["templates"][0]
        self.assertEqual("default", template)
        self.assertDictEqual({
          "template": {
            "settings": {
              "index.number_of_shards": 1,
              "index.number_of_replicas": 1
            },
            "mappings": {
              "properties": {
                "@timestamp": {
                  "type": "date"
                }
              }
            }
          }
        }, body)


class DeleteComponentTemplateParamSource(TestCase):
    def test_delete_index_template_by_name(self):
        source = params.DeleteComponentTemplateParamSource(workload.Workload(name="unit-test"), params={"template": "default"})
        p = source.params()
        self.assertEqual(1, len(p["templates"]))
        self.assertEqual("default", p["templates"][0])
        self.assertTrue(p["only-if-exists"])
        self.assertDictEqual({}, p["request-params"])

    def test_delete_index_template_no_name(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.DeleteComponentTemplateParamSource(workload.Workload(name="unit-test"),
                                                  params={"operation-type": "delete-component-template"})
        self.assertEqual("Please set the property 'template' for the delete-component-template operation.",
                         ctx.exception.args[0])

    def test_delete_index_template_from_workload(self):
        tpl1 = workload.ComponentTemplate(name="logs", content={
          "template": {
            "mappings": {
              "properties": {
                "@timestamp": {
                  "type": "date"
                }
              }
            }
          }
        })
        tpl2 = workload.ComponentTemplate(name="metrics", content={
          "template": {
            "settings": {
              "index.number_of_shards": 1,
              "index.number_of_replicas": 1
            }
          }
        })
        source = params.DeleteComponentTemplateParamSource(workload.Workload(name="unit-test", templates=[tpl1, tpl2]), params={
            "request-params": {
                "master_timeout": 20
            },
            "only-if-exists": False
        })

        p = source.params()

        self.assertEqual(2, len(p["templates"]))
        self.assertEqual("logs", p["templates"][0])
        self.assertEqual("metrics", p["templates"][1])
        self.assertFalse(p["only-if-exists"])
        self.assertDictEqual({"master_timeout": 20}, p["request-params"])


class SearchParamSourceTests(TestCase):
    def test_passes_cache(self):
        index1 = workload.Index(name="index1", types=["type1"])

        source = params.SearchParamSource(workload=workload.Workload(name="unit-test", indices=[index1]), params={
            "body": {
                "query": {
                    "match_all": {}
                }
            },
            "headers": {
                "header1": "value1"
            },
            "cache": True
        })
        p = source.params()

        self.assertEqual(11, len(p))
        self.assertEqual(True, p["calculate-recall"])
        self.assertEqual("index1", p["index"])
        self.assertIsNone(p["type"])
        self.assertIsNone(p["request-timeout"])
        self.assertIsNone(p["opaque-id"])
        self.assertDictEqual({"header1": "value1"}, p["headers"])
        self.assertEqual({}, p["request-params"])
        # Explicitly check in these tests for equality - assertFalse would also succeed if it is `None`.
        self.assertEqual(True, p["cache"])
        self.assertEqual(True, p["response-compression-enabled"])
        self.assertEqual(False, p["detailed-results"])
        self.assertEqual({
            "query": {
                "match_all": {}
            }
        }, p["body"])

    def test_uses_data_stream(self):
        ds1 = workload.DataStream(name="data-stream-1")
        source = params.SearchParamSource(workload=workload.Workload(name="unit-test", data_streams=[ds1]), params={
            "body": {
                "query": {
                    "match_all": {}
                }
            },
            "request-timeout": 1.0,
            "headers": {
                "header1": "value1",
                "header2": "value2"
            },
            "opaque-id": "12345abcde",
            "cache": True
        })
        p = source.params()

        self.assertEqual(11, len(p))
        self.assertEqual(True, p["calculate-recall"])
        self.assertEqual("data-stream-1", p["index"])
        self.assertIsNone(p["type"])
        self.assertEqual(1.0, p["request-timeout"])
        self.assertDictEqual({
            "header1": "value1",
            "header2": "value2"
        }, p["headers"])
        self.assertEqual("12345abcde", p["opaque-id"])
        self.assertEqual({}, p["request-params"])
        self.assertEqual(True, p["cache"])
        self.assertEqual(True, p["response-compression-enabled"])
        self.assertEqual(False, p["detailed-results"])
        self.assertEqual({
            "query": {
                "match_all": {}
            }
        }, p["body"])

    def test_create_without_index(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            params.SearchParamSource(workload=workload.Workload(name="unit-test"), params={
                "type": "type1",
                "body": {
                    "query": {
                        "match_all": {}
                    }
                }
            }, operation_name="test_operation")

        self.assertEqual("'index' or 'data-stream' is mandatory and is missing for operation 'test_operation'", ctx.exception.args[0])

    def test_passes_request_parameters(self):
        index1 = workload.Index(name="index1", types=["type1"])

        source = params.SearchParamSource(workload=workload.Workload(name="unit-test", indices=[index1]), params={
            "request-params": {
                "_source_include": "some_field"
            },
            "body": {
                "query": {
                    "match_all": {}
                }
            }
        })
        p = source.params()

        self.assertEqual(11, len(p))
        self.assertEqual(True, p["calculate-recall"])
        self.assertEqual("index1", p["index"])
        self.assertIsNone(p["type"])
        self.assertIsNone(p["request-timeout"])
        self.assertIsNone(p["headers"])
        self.assertIsNone(p["opaque-id"])
        self.assertEqual({
            "_source_include": "some_field"
        }, p["request-params"])
        self.assertIsNone(p["cache"])
        self.assertEqual(True, p["response-compression-enabled"])
        self.assertEqual(False, p["detailed-results"])
        self.assertEqual({
            "query": {
                "match_all": {}
            }
        }, p["body"])

    def test_user_specified_overrides_defaults(self):
        index1 = workload.Index(name="index1", types=["type1"])

        source = params.SearchParamSource(workload=workload.Workload(name="unit-test", indices=[index1]), params={
            "index": "_all",
            "type": "type1",
            "cache": False,
            "response-compression-enabled": False,
            "detailed-results": True,
            "opaque-id": "12345abcde",
            "body": {
                "query": {
                    "match_all": {}
                }
            }
        })
        p = source.params()

        self.assertEqual(11, len(p))
        self.assertEqual(True, p["calculate-recall"])
        self.assertEqual("_all", p["index"])
        self.assertEqual("type1", p["type"])
        self.assertDictEqual({}, p["request-params"])
        self.assertIsNone(p["request-timeout"])
        self.assertIsNone(p["headers"])
        self.assertEqual("12345abcde", p["opaque-id"])
        # Explicitly check for equality to `False` - assertFalse would also succeed if it is `None`.
        self.assertEqual(False, p["cache"])
        self.assertEqual(False, p["response-compression-enabled"])
        self.assertEqual(True, p["detailed-results"])
        self.assertEqual({
            "query": {
                "match_all": {}
            }
        }, p["body"])

    def test_user_specified_data_stream_overrides_defaults(self):
        ds1 = workload.DataStream(name="data-stream-1")

        source = params.SearchParamSource(workload=workload.Workload(name="unit-test", data_streams=[ds1]), params={
            "data-stream": "data-stream-2",
            "cache": False,
            "response-compression-enabled": False,
            "request-timeout": 1.0,
            "body": {
                "query": {
                    "match_all": {}
                }
            }
        })
        p = source.params()

        self.assertEqual(11, len(p))
        self.assertEqual(True, p["calculate-recall"])
        self.assertEqual("data-stream-2", p["index"])
        self.assertIsNone(p["type"])
        self.assertEqual(1.0, p["request-timeout"])
        self.assertIsNone(p["headers"])
        self.assertIsNone(p["opaque-id"])
        self.assertDictEqual({}, p["request-params"])
        # Explicitly check for equality to `False` - assertFalse would also succeed if it is `None`.
        self.assertEqual(False, p["cache"])
        self.assertEqual(False, p["response-compression-enabled"])
        self.assertEqual(False, p["detailed-results"])
        self.assertEqual({
            "query": {
                "match_all": {}
            }
        }, p["body"])

    def test_invalid_data_stream_with_type(self):
        with self.assertRaises(exceptions.InvalidSyntax) as ctx:
            ds1 = workload.DataStream(name="data-stream-1")

            params.SearchParamSource(workload=workload.Workload(name="unit-test", data_streams=[ds1]), params={
                "data-stream": "data-stream-2",
                "type": "_doc",
                "cache": False,
                "response-compression-enabled": False,
                "body": {
                    "query": {
                        "match_all": {}
                    }
                }
            }, operation_name="test_operation")

        self.assertEqual("'type' not supported with 'data-stream' for operation 'test_operation'",
                         ctx.exception.args[0])

    def test_assertions_without_detailed_results_are_invalid(self):
        index1 = workload.Index(name="index1", types=["type1"])
        with self.assertRaisesRegex(exceptions.InvalidSyntax,
                                    r"The property \[detailed-results\] must be \[true\] if assertions are defined"):
            params.SearchParamSource(workload=workload.Workload(name="unit-test", indices=[index1]), params={
                "index": "_all",
                # unset!
                #"detailed-results": True,
                "assertions": [{
                    "property": "hits",
                    "condition": ">",
                    "value": 0
                }],
                "body": {
                    "query": {
                        "match_all": {}
                    }
                }
            })


class ForceMergeParamSourceTests(TestCase):
    def test_force_merge_index_from_workload(self):
        source = params.ForceMergeParamSource(workload.Workload(name="unit-test", indices=[
            workload.Index(name="index1"),
            workload.Index(name="index2"),
            workload.Index(name="index3")
        ]), params={})

        p = source.params()

        self.assertEqual("index1,index2,index3", p["index"])
        self.assertEqual("blocking", p["mode"])

    def test_force_merge_data_stream_from_workload(self):
        source = params.ForceMergeParamSource(workload.Workload(name="unit-test", data_streams=[
            workload.DataStream(name="data-stream-1"),
            workload.DataStream(name="data-stream-2"),
            workload.DataStream(name="data-stream-3")
        ]), params={})

        p = source.params()

        self.assertEqual("data-stream-1,data-stream-2,data-stream-3", p["index"])
        self.assertEqual("blocking", p["mode"])

    def test_force_merge_index_by_name(self):
        source = params.ForceMergeParamSource(workload.Workload(name="unit-test"), params={"index": "index2"})

        p = source.params()

        self.assertEqual("index2", p["index"])
        self.assertEqual("blocking", p["mode"])

    def test_force_merge_by_data_stream_name(self):
        source = params.ForceMergeParamSource(workload.Workload(name="unit-test"), params={"data-stream": "data-stream-2"})

        p = source.params()

        self.assertEqual("data-stream-2", p["index"])
        self.assertEqual("blocking", p["mode"])

    def test_default_force_merge_index(self):
        source = params.ForceMergeParamSource(workload.Workload(name="unit-test"), params={})

        p = source.params()

        self.assertEqual("_all", p["index"])
        self.assertEqual("blocking", p["mode"])

    def test_force_merge_all_params(self):
        source = params.ForceMergeParamSource(workload.Workload(name="unit-test"), params={"index": "index2",
                                                                                     "request-timeout": 30,
                                                                                     "max-num-segments": 1,
                                                                                     "polling-period": 20,
                                                                                     "mode": "polling"})

        p = source.params()

        self.assertEqual("index2", p["index"])
        self.assertEqual(30, p["request-timeout"])
        self.assertEqual(1, p["max-num-segments"])
        self.assertEqual("polling", p["mode"])


class VectorSearchParamSourceTests(TestCase):
    DEFAULT_INDEX_NAME = "test-index"
    DEFAULT_FIELD_NAME = "test-field"
    DEFAULT_CONTEXT = Context.INDEX
    DEFAULT_TYPE = HDF5DataSet.FORMAT_NAME
    DEFAULT_NUM_VECTORS = 10
    DEFAULT_DIMENSION = 10
    DEFAULT_RANDOM_STRING_LENGTH = 8

    def setUp(self) -> None:
        self.data_set_dir = tempfile.mkdtemp()

        # Create a data set we know to be valid for convenience
        self.valid_data_set_path = create_data_set(
            self.DEFAULT_NUM_VECTORS,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            self.DEFAULT_CONTEXT,
            self.data_set_dir
        )

    def tearDown(self):
        shutil.rmtree(self.data_set_dir)

    def test_missing_params(self):
        empty_params = dict()
        self.assertRaises(
            ConfigurationError,
            lambda: self.TestVectorsFromDataSetParamSource(
                workload.Workload(name="unit-test"),
                empty_params, VectorSearchParamSourceTests.DEFAULT_CONTEXT)
        )

    def test_invalid_data_set_format(self):
        invalid_data_set_format = "invalid-data-set-format"

        test_param_source_params = {
            "index": VectorSearchParamSourceTests.DEFAULT_INDEX_NAME,
            "field": VectorSearchParamSourceTests.DEFAULT_FIELD_NAME,
            "data_set_format": invalid_data_set_format,
            "data_set_path": self.valid_data_set_path,
        }
        self.assertRaises(
            ConfigurationError,
            lambda: self.TestVectorsFromDataSetParamSource(
                workload.Workload(name="unit-test"),
                test_param_source_params,
                self.DEFAULT_CONTEXT
            ).partition(0, 1)
        )

    def test_corpus_not_found_in_workload(self):
        corpora = [
            workload.DocumentCorpus(name="sift-128", documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_HDF5,number_of_documents=10)
            ]),
        ]
        test_param_source_params = {
            "index": VectorSearchParamSourceTests.DEFAULT_INDEX_NAME,
            "field": VectorSearchParamSourceTests.DEFAULT_FIELD_NAME,
            "data_set_format": "hdf5",
            "data_set_corpus": "sift-128-1"
        }
        self.assertRaises(
            ConfigurationError,
            lambda: self.TestVectorsFromDataSetParamSource(
                workload.Workload(name="unit-test", corpora=corpora),
                test_param_source_params,
                self.DEFAULT_CONTEXT
            ).partition(0, 1)
        )

    def test_corpus_contains_more_than_one_files(self):
        corpus_name="sift-128"
        corpora = [
            workload.DocumentCorpus(name=corpus_name, documents=[
                workload.Documents(
                    source_format=workload.Documents.SOURCE_FORMAT_HDF5,
                    number_of_documents=10,
                    document_file="file1"
                ),
                workload.Documents(
                    source_format=workload.Documents.SOURCE_FORMAT_HDF5,
                    number_of_documents=10,
                    document_file="file2"
                )
            ]),
        ]
        test_param_source_params = {
            "index": VectorSearchParamSourceTests.DEFAULT_INDEX_NAME,
            "field": VectorSearchParamSourceTests.DEFAULT_FIELD_NAME,
            "data_set_format": "hdf5",
            "data_set_corpus": corpus_name,
        }
        self.assertRaises(
            ConfigurationError,
            lambda: self.TestVectorsFromDataSetParamSource(
                workload.Workload(name="unit-test", corpora=corpora),
                test_param_source_params,
                self.DEFAULT_CONTEXT
            ).partition(0, 1)
        )

    def test_missing_data_set_path_or_corpus(self):
        test_param_source_params = {
            "index": VectorSearchParamSourceTests.DEFAULT_INDEX_NAME,
            "field": VectorSearchParamSourceTests.DEFAULT_FIELD_NAME,
            "data_set_format": "hdf5",
        }
        self.assertRaises(
            ConfigurationError,
            lambda: self.TestVectorsFromDataSetParamSource(
                workload.Workload(name="unit-test"),
                test_param_source_params,
                self.DEFAULT_CONTEXT
            ).partition(0, 1)
        )

    def test_either_data_set_path_or_corpus(self):
        test_param_source_params = {
            "index": VectorSearchParamSourceTests.DEFAULT_INDEX_NAME,
            "field": VectorSearchParamSourceTests.DEFAULT_FIELD_NAME,
            "data_set_format": "hdf5",
            "data_set_corpus": "corpus_name",
            "data_set_path": "file-path",
        }
        self.assertRaises(
            ConfigurationError,
            lambda: self.TestVectorsFromDataSetParamSource(
                workload.Workload(name="unit-test"),
                test_param_source_params,
                self.DEFAULT_CONTEXT
            )
        )

    def test_missing_corpus(self):
        test_param_source_params = {
            "index": VectorSearchParamSourceTests.DEFAULT_INDEX_NAME,
            "field": VectorSearchParamSourceTests.DEFAULT_FIELD_NAME,
            "data_set_format": "hdf5",
            "data_set_corpus": "sift-128"
        }
        self.assertRaises(
            ConfigurationError,
            lambda: self.TestVectorsFromDataSetParamSource(
                workload.Workload(name="unit-test", corpora=[]),
                test_param_source_params,
                self.DEFAULT_CONTEXT
            ).partition(0, 1)
        )

    def test_invalid_data_set_path(self):
        invalid_data_set_path = "invalid-data-set-path"
        test_param_source_params = {
            "index": self.DEFAULT_INDEX_NAME,
            "field": self.DEFAULT_FIELD_NAME,
            "data_set_format": HDF5DataSet.FORMAT_NAME,
            "data_set_path": invalid_data_set_path,
        }
        self.assertRaises(
            FileNotFoundError,
            lambda: self.TestVectorsFromDataSetParamSource(
                workload.Workload(name="unit-test"),
                test_param_source_params,
                self.DEFAULT_CONTEXT
            ).partition(0, 1)
        )

    def test_partition_hdf5_corpus(self):
        num_vectors = 100
        num_partitions = 10
        corpus_name = "random-hdf5-corpus"

        hdf5_data_set_path = create_data_set(
            num_vectors,
            self.DEFAULT_DIMENSION,
            HDF5DataSet.FORMAT_NAME,
            self.DEFAULT_CONTEXT,
            self.data_set_dir
        )
        corpora = [
            workload.DocumentCorpus(name=corpus_name, documents=[
                workload.Documents(source_format=workload.Documents.SOURCE_FORMAT_HDF5,
                                   number_of_documents=num_vectors,
                                   document_file=hdf5_data_set_path)
            ]),
        ]

        test_param_source_params = {
            "index": self.DEFAULT_INDEX_NAME,
            "field": self.DEFAULT_FIELD_NAME,
            "data_set_format": HDF5DataSet.FORMAT_NAME,
            "data_set_corpus": corpus_name,
        }
        test_param_source = self.TestVectorsFromDataSetParamSource(
            workload.Workload(name="unit-test", corpora=corpora),
            test_param_source_params,
            self.DEFAULT_CONTEXT
        )

        vectors_per_partition = num_vectors // num_partitions

        self._test_partition(
            test_param_source,
            num_partitions,
            vectors_per_partition
        )

    def test_partition_hdf5(self):
        num_vectors = 100
        num_partitions = 10

        hdf5_data_set_path = create_data_set(
            num_vectors,
            self.DEFAULT_DIMENSION,
            HDF5DataSet.FORMAT_NAME,
            self.DEFAULT_CONTEXT,
            self.data_set_dir
        )

        test_param_source_params = {
            "index": self.DEFAULT_INDEX_NAME,
            "field": self.DEFAULT_FIELD_NAME,
            "data_set_format": HDF5DataSet.FORMAT_NAME,
            "data_set_path": hdf5_data_set_path,
        }
        test_param_source = self.TestVectorsFromDataSetParamSource(
            workload.Workload(name="unit-test"),
            test_param_source_params,
            self.DEFAULT_CONTEXT
        )

        vectors_per_partition = num_vectors // num_partitions

        self._test_partition(
            test_param_source,
            num_partitions,
            vectors_per_partition
        )

    def test_partition_bigann(self):
        num_vectors = 100
        num_partitions = 10
        float_extension = "fbin"

        bigann_data_set_path = create_data_set(
            num_vectors,
            self.DEFAULT_DIMENSION,
            float_extension,
            self.DEFAULT_CONTEXT,
            self.data_set_dir
        )

        test_param_source_params = {
            "index": self.DEFAULT_INDEX_NAME,
            "field": self.DEFAULT_FIELD_NAME,
            "data_set_format": "bigann",
            "data_set_path": bigann_data_set_path,
        }
        test_param_source = self.TestVectorsFromDataSetParamSource(
            workload.Workload(name="unit-test"),
            test_param_source_params,
            self.DEFAULT_CONTEXT
        )
        vectors_per_partition = num_vectors // num_partitions
        self._test_partition(
            test_param_source,
            num_partitions,
            vectors_per_partition
        )

    def _test_partition(
            self,
            test_param_source: VectorDataSetPartitionParamSource,
            num_partitions: int,
            vec_per_partition: int
    ):
        for i in range(num_partitions):
            test_param_source_i = test_param_source.partition(i, num_partitions)
            self.assertEqual(test_param_source_i.num_vectors, vec_per_partition)
            self.assertEqual(test_param_source_i.offset, i * vec_per_partition)

    class TestVectorsFromDataSetParamSource(VectorDataSetPartitionParamSource):
        """
        Empty implementation of ABC VectorsFromDataSetParamSource so that we can
        test the concrete methods.
        """

        def params(self):
            pass


class VectorSearchPartitionPartitionParamSourceTestCase(TestCase):

    DEFAULT_INDEX_NAME = "test-partition-index"
    DEFAULT_FIELD_NAME = "test-vector-field"
    DEFAULT_CONTEXT = Context.INDEX
    DEFAULT_TYPE = HDF5DataSet.FORMAT_NAME
    DEFAULT_NUM_VECTORS = 10
    DEFAULT_DIMENSION = 10
    DEFAULT_RANDOM_STRING_LENGTH = 8

    def setUp(self) -> None:
        self.data_set_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.data_set_dir)

    def test_params_default(self):
        # Create a data set
        k = 12
        data_set_path = create_data_set(
            self.DEFAULT_NUM_VECTORS,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.QUERY,
            self.data_set_dir
        )
        create_data_set(
            self.DEFAULT_NUM_VECTORS,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.NEIGHBORS,
            self.data_set_dir,
            data_set_path
        )

        # Create a QueryVectorsFromDataSetParamSource with relevant params
        test_param_source_params = {
            "field": self.DEFAULT_FIELD_NAME,
            "data_set_format": self.DEFAULT_TYPE,
            "data_set_path": data_set_path,
            "k": k
        }
        query_param_source = VectorSearchPartitionParamSource(
            workload.Workload(name="unit-test"),
            test_param_source_params, {
                "index": self.DEFAULT_INDEX_NAME,
                "request-params": {},
            }
        )
        query_param_source_partition = query_param_source.partition(0, 1)

        # Check each
        for _ in range(DEFAULT_NUM_VECTORS):
            self._check_params(
                query_param_source_partition.params(),
                self.DEFAULT_FIELD_NAME,
                self.DEFAULT_DIMENSION,
                k,
            )

        # Assert last call creates stop iteration
        with self.assertRaises(StopIteration):
            query_param_source_partition.params()

    def test_post_filter(self):
        # Create a data set
        k = 12
        data_set_path = create_data_set(
            self.DEFAULT_NUM_VECTORS,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.QUERY,
            self.data_set_dir
        )
        neighbors_data_set_path = create_data_set(
            self.DEFAULT_NUM_VECTORS,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.NEIGHBORS,
            self.data_set_dir,
        )

        # Create a QueryVectorsFromDataSetParamSource with relevant params

        POST_FILTER_BODY = {"range": {"price": {"gte": 5, "lte": 10}}}
        test_param_source_params = {
            "field": self.DEFAULT_FIELD_NAME,
            "data_set_format": self.DEFAULT_TYPE,
            "data_set_path": data_set_path,
            "neighbors_data_set_path": neighbors_data_set_path,
            "k": k,
            "filter_type": "post_filter",
            "filter_body": POST_FILTER_BODY,
        }
        query_param_source = VectorSearchPartitionParamSource(
            workload.Workload(name="unit-test"),
            test_param_source_params,
            {
                "index": self.DEFAULT_INDEX_NAME,
                "request-params": {},
                "body": {
                    "size": 100,
                },
            },
        )
        query_param_source_partition = query_param_source.partition(0, 1)

        # Check each
        for _ in range(DEFAULT_NUM_VECTORS):
            params = query_param_source_partition.params()
            self._check_params(
                params,
                self.DEFAULT_FIELD_NAME,
                self.DEFAULT_DIMENSION,
                k,
                100,
            )
            post_filter = params.get("body").get("post_filter")
            self.assertIsInstance(post_filter, dict)
            self.assertEqual(post_filter, POST_FILTER_BODY)

        # Assert last call creates stop iteration
        with self.assertRaises(StopIteration):
            query_param_source_partition.params()

    def test_bool_filter(self):
        # Create a data set
        k = 12
        data_set_path = create_data_set(
            self.DEFAULT_NUM_VECTORS,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.QUERY,
            self.data_set_dir,
        )
        neighbors_data_set_path = create_data_set(
            self.DEFAULT_NUM_VECTORS,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.NEIGHBORS,
            self.data_set_dir,
        )
        # Create a QueryVectorsFromDataSetParamSource with relevant params

        BOOL_FILTER_BODY = {
            "bool": {
                "must": [
                    {"range": {"rating": {"gte": 8, "lte": 10}}},
                    {"term": {"parking": "true"}},
                ]
            }
        }
        test_param_source_params = {
            "field": self.DEFAULT_FIELD_NAME,
            "data_set_format": self.DEFAULT_TYPE,
            "data_set_path": data_set_path,
            "neighbors_data_set_path": neighbors_data_set_path,
            "k": k,
            "filter_type": "boolean",
            "filter_body": BOOL_FILTER_BODY,
        }
        query_param_source = VectorSearchPartitionParamSource(
            workload.Workload(name="unit-test"),
            test_param_source_params,
            {
                "index": self.DEFAULT_INDEX_NAME,
                "request-params": {},
                "body": {
                    "size": 100,
                },
            },
        )
        query_param_source_partition = query_param_source.partition(0, 1)

        # Check each
        for _ in range(DEFAULT_NUM_VECTORS):
            params = query_param_source_partition.params()
            self._check_params_bool(
                params,
                self.DEFAULT_FIELD_NAME,
                self.DEFAULT_DIMENSION,
                k,
                100,
                BOOL_FILTER_BODY,
            )
            # post_filter = params.get("body").get("post_filter")
            # self.assertIsInstance(post_filter, dict)
            # self.assertEqual(post_filter, BOOL_FILTER_BODY)

        # Assert last call creates stop iteration
        with self.assertRaises(StopIteration):
            query_param_source_partition.params()

    def test_script_score_filter(self):
        # Create a data set
        k = 12
        data_set_path = create_data_set(
            self.DEFAULT_NUM_VECTORS,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.QUERY,
            self.data_set_dir,
        )
        neighbors_data_set_path = create_data_set(
            self.DEFAULT_NUM_VECTORS,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.NEIGHBORS,
            self.data_set_dir,
        )

        # Create a QueryVectorsFromDataSetParamSource with relevant params

        SCRIPT_SCORE_FILTER_BODY = {
            "bool": {
                "must": [
                    {"range": {"rating": {"gte": 8, "lte": 10}}},
                    {"term": {"parking": "true"}},
                ]
            }
        }
        test_param_source_params = {
            "field": self.DEFAULT_FIELD_NAME,
            "data_set_format": self.DEFAULT_TYPE,
            "data_set_path": data_set_path,
            "neighbors_data_set_path": neighbors_data_set_path,
            "k": k,
            "filter_type": "script",
            "filter_body": SCRIPT_SCORE_FILTER_BODY,
        }
        query_param_source = VectorSearchPartitionParamSource(
            workload.Workload(name="unit-test"),
            test_param_source_params,
            {
                "index": self.DEFAULT_INDEX_NAME,
                "request-params": {},
                "body": {
                    "size": 100,
                },
            },
        )
        query_param_source_partition = query_param_source.partition(0, 1)

        # Check each
        for _ in range(DEFAULT_NUM_VECTORS):
            params = query_param_source_partition.params()
            self._check_params_script_score(
                params,
                self.DEFAULT_FIELD_NAME,
                self.DEFAULT_DIMENSION,
                k,
                100,
                SCRIPT_SCORE_FILTER_BODY,
            )
            # post_filter = params.get("body").get("post_filter")
            # self.assertIsInstance(post_filter, dict)
            # self.assertEqual(post_filter, BOOL_FILTER_BODY)

        # Assert last call creates stop iteration
        with self.assertRaises(StopIteration):
            query_param_source_partition.params()

    def _check_params(
            self,
            actual_params: dict,
            expected_field: str,
            expected_dimension: int,
            expected_k: int,
            expected_size=None,
            expected_filter=None,
    ):
        body = actual_params.get("body")
        self.assertIsInstance(body, dict)
        query = body.get("query")
        self.assertIsInstance(query, dict)
        query_knn = query.get("knn")
        self.assertIsInstance(query_knn, dict)
        field = query_knn.get(expected_field)
        self.assertIsInstance(field, dict)
        vector = field.get("vector")
        self.assertIsInstance(vector, np.ndarray)
        self.assertEqual(len(list(vector)), expected_dimension)
        k = field.get("k")
        self.assertEqual(k, expected_k)
        neighbor = actual_params.get("neighbors")
        self.assertIsInstance(neighbor, list)
        self.assertEqual(len(neighbor), expected_dimension)
        size = body.get("size")
        self.assertEqual(size, expected_size if expected_size else expected_k)
        self.assertEqual(field.get("filter"), expected_filter)

    def _check_params_bool(
        self,
            actual_params: dict,
            expected_field: str,
            expected_dimension: int,
            expected_k: int,
            expected_size=None,
            expected_bool_query=None,
            check_vectors=True,
            ):
        body = actual_params.get("body")
        self.assertIsInstance(body, dict)
        query = body.get("query")
        self.assertIsInstance(query, dict)
        query_bool = query.get("bool")
        self.assertIsInstance(query_bool, dict)
        filter = query_bool.get("filter")
        self.assertIsInstance(filter, dict)
        self.assertEqual(filter, expected_bool_query)

        must_clause = query_bool.get("must")
        self.assertIsInstance(must_clause, list)

        if check_vectors:
            knn_dict = must_clause[0]

            repacked = {"body": {"query": knn_dict, "size": body.get("size") },
                        "neighbors": actual_params.get("neighbors")
                        }

            self._check_params(repacked, expected_field, expected_dimension, expected_k,expected_size)

    def _check_params_script_score(
                                           self,
            actual_params: dict,
            expected_field: str,
            expected_dimension: int,
            expected_k: int,
            expected_size=None,
            expected_script_query=None
            ):
        body = actual_params.get("body")
        self.assertIsInstance(body, dict)
        query = body.get("query")
        self.assertIsInstance(query, dict)
        script_score_query = query.get("script_score")
        self.assertIsInstance(script_score_query, dict)
        bool_from_script_score = script_score_query.get("query").get("bool").get("filter")

        self.assertEqual(bool_from_script_score, expected_script_query)

        script = script_score_query.get("script")
        self.assertIsInstance(script, dict)

        source = script.get("source")
        self.assertEqual(source, "knn_score")

        lang = script.get("lang")
        self.assertEqual(lang, "knn")

        params = script.get("params")
        self.assertIsInstance(params, dict)

        field = params.get("field")
        self.assertEqual(field, expected_field)

        vector = params.get("query_value")
        self.assertIsInstance(vector, np.ndarray)
        self.assertEqual(len(list(vector)), expected_dimension)

        space_type = params.get("space_type")
        self.assertEqual(space_type, "l2") # TODO change this once it's all modifiable.

class BulkVectorsFromDataSetParamSourceTestCase(TestCase):

    DEFAULT_INDEX_NAME = "test-partition-index"
    DEFAULT_VECTOR_FIELD_NAME = "test-vector-field"
    DEFAULT_CONTEXT = Context.INDEX
    DEFAULT_TYPE = HDF5DataSet.FORMAT_NAME
    DEFAULT_NUM_VECTORS = 10
    DEFAULT_DIMENSION = 10
    DEFAULT_RANDOM_STRING_LENGTH = 8
    DEFAULT_ID_FIELD_NAME = "_id"

    def setUp(self) -> None:
        self.data_set_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.data_set_dir)

    def test_params_default(self):
        num_vectors = 49
        bulk_size = 10
        data_set_path = create_data_set(
            num_vectors,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.INDEX,
            self.data_set_dir
        )

        test_param_source_params = {
            "index": self.DEFAULT_INDEX_NAME,
            "field": self.DEFAULT_VECTOR_FIELD_NAME,
            "data_set_format": self.DEFAULT_TYPE,
            "data_set_path": data_set_path,
            "bulk_size": bulk_size,
            "id-field-name": self.DEFAULT_ID_FIELD_NAME,
        }
        bulk_param_source = BulkVectorsFromDataSetParamSource(
            workload.Workload(name="unit-test"), test_param_source_params)
        bulk_param_source_partition = bulk_param_source.partition(0, 1)
        # Check each payload returned
        vectors_consumed = 0
        while vectors_consumed < num_vectors:
            expected_num_vectors = min(num_vectors - vectors_consumed, bulk_size)
            actual_params = bulk_param_source_partition.params()
            self._check_params(
                actual_params,
                self.DEFAULT_INDEX_NAME,
                self.DEFAULT_VECTOR_FIELD_NAME,
                self.DEFAULT_DIMENSION,
                expected_num_vectors,
                self.DEFAULT_ID_FIELD_NAME,
            )
            vectors_consumed += expected_num_vectors

        # Assert last call creates stop iteration
        with self.assertRaises(StopIteration):
            bulk_param_source_partition.params()

    def test_params_custom(self):
        num_vectors = 49
        bulk_size = 10
        data_set_path = create_data_set(
            num_vectors,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.INDEX,
            self.data_set_dir
        )

        test_param_source_params = {
            "index": self.DEFAULT_INDEX_NAME,
            "field": self.DEFAULT_VECTOR_FIELD_NAME,
            "data_set_format": self.DEFAULT_TYPE,
            "data_set_path": data_set_path,
            "bulk_size": bulk_size,
            "id-field-name": "id",
        }
        bulk_param_source = BulkVectorsFromDataSetParamSource(
            workload.Workload(name="unit-test"), test_param_source_params)
        bulk_param_source_partition = bulk_param_source.partition(0, 1)
        # Check each payload returned
        vectors_consumed = 0
        while vectors_consumed < num_vectors:
            expected_num_vectors = min(num_vectors - vectors_consumed, bulk_size)
            actual_params = bulk_param_source_partition.params()
            self._check_params(
                actual_params,
                self.DEFAULT_INDEX_NAME,
                self.DEFAULT_VECTOR_FIELD_NAME,
                self.DEFAULT_DIMENSION,
                expected_num_vectors,
                "id",
            )
            vectors_consumed += expected_num_vectors

        # Assert last call creates stop iteration
        with self.assertRaises(StopIteration):
            bulk_param_source_partition.params()

    def _check_params(
            self,
            actual_params: dict,
            expected_index: str,
            expected_vector_field: str,
            expected_dimension: int,
            expected_num_vectors_in_payload: int,
            expected_id_field: str,
    ):
        size = actual_params.get("size")
        self.assertEqual(size, expected_num_vectors_in_payload)
        body = actual_params.get("body")
        self.assertIsInstance(body, list)
        self.assertEqual(len(body) // 2, expected_num_vectors_in_payload)

        # Bulk payload has 2 parts: first one is the header and the second one
        # is the body. The header will have the index name and the body will
        # have the vector
        for header, req_body in zip(*[iter(body)] * 2):
            index = header.get("index")
            self.assertIsInstance(index, dict)

            index_name = index.get("_index")
            self.assertEqual(index_name, expected_index)

            vector = req_body.get(expected_vector_field)
            self.assertIsInstance(vector, list)
            self.assertEqual(len(vector), expected_dimension)
            if expected_id_field in index:
                self.assertEqual(self.DEFAULT_ID_FIELD_NAME, expected_id_field)
                self.assertFalse(expected_id_field in req_body)
                continue
            self.assertTrue(expected_id_field in req_body)


class BulkVectorsAttributeCase(TestCase):
    DEFAULT_INDEX_NAME = "test-partition-index"
    DEFAULT_VECTOR_FIELD_NAME = "test-vector-field"
    DEFAULT_CONTEXT = Context.INDEX
    DEFAULT_TYPE = HDF5DataSet.FORMAT_NAME
    DEFAULT_NUM_VECTORS = 10
    DEFAULT_DIMENSION = 10
    DEFAULT_RANDOM_STRING_LENGTH = 8
    DEFAULT_ID_FIELD_NAME = "_id"
    ATTRIBUTES_LIST = ['taste', 'color', 'age']

    def setUp(self) -> None:
        self.data_set_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.data_set_dir)

    def test_params_efficient_filter(
        self
    ):
        num_vectors = 49
        bulk_size = 10
        data_set_path = create_data_set(
            num_vectors,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.INDEX,
            self.data_set_dir
        )
        parent_data_set_path = create_attributes_data_set(
            num_vectors,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.ATTRIBUTES,
            self.data_set_dir,
        )

        test_param_source_params = {
            "index": self.DEFAULT_INDEX_NAME,
            "field": self.DEFAULT_VECTOR_FIELD_NAME,
            "data_set_format": self.DEFAULT_TYPE,
            "data_set_path": data_set_path,
            "bulk_size": bulk_size,
            "id-field-name": self.DEFAULT_ID_FIELD_NAME,
            "filter_attributes": self.ATTRIBUTES_LIST
        }
        bulk_param_source = BulkVectorsFromDataSetParamSource(
            workload.Workload(name="unit-test"), test_param_source_params
        )
        bulk_param_source.parent_data_set_path = parent_data_set_path
        bulk_param_source_partition = bulk_param_source.partition(0, 1)
        # Check each payload returned
        vectors_consumed = 0
        while vectors_consumed < num_vectors:
            expected_num_vectors = min(num_vectors - vectors_consumed, bulk_size)
            actual_params = bulk_param_source_partition.params()
            self._check_params_attributes(
                actual_params,
                self.DEFAULT_INDEX_NAME,
                self.DEFAULT_VECTOR_FIELD_NAME,
                self.DEFAULT_DIMENSION,
                expected_num_vectors,
                self.DEFAULT_ID_FIELD_NAME,
            )
            vectors_consumed += expected_num_vectors

        # Assert last call creates stop iteration
        with self.assertRaises(StopIteration):
            bulk_param_source_partition.params()

    def _check_params_attributes(
            self,
        actual_params: dict,
        expected_index: str,
        expected_vector_field: str,
        expected_dimension: int,
        expected_num_vectors_in_payload: int,
        expected_id_field: str,
    ):
        size = actual_params.get("size")
        self.assertEqual(size, expected_num_vectors_in_payload)
        body = actual_params.get("body")
        self.assertIsInstance(body, list)
        self.assertEqual(len(body) // 2, expected_num_vectors_in_payload)

        # Bulk payload has 2 parts: first one is the header and the second one
        # is the body. The header will have the index name and the body will
        # have the vector
        for header, req_body in zip(*[iter(body)] * 2):
            index = header.get("index")
            self.assertIsInstance(index, dict)

            index_name = index.get("_index")
            self.assertEqual(index_name, expected_index)

            vector = req_body.get(expected_vector_field)
            self.assertIsInstance(vector, list)
            self.assertEqual(len(vector), expected_dimension)

            for attribute in self.ATTRIBUTES_LIST:
                self.assertTrue(attribute in req_body)
            if expected_id_field in index:
                self.assertEqual(self.DEFAULT_ID_FIELD_NAME, expected_id_field)
                self.assertFalse(expected_id_field in req_body)
                continue
            self.assertTrue(expected_id_field in req_body)


class VectorsNestedCase(TestCase):
    DEFAULT_INDEX_NAME = "test-partition-index"
    DEFAULT_VECTOR_FIELD_NAME = "nested.test-vector-field"
    DEFAULT_CONTEXT = Context.INDEX
    DEFAULT_TYPE = HDF5DataSet.FORMAT_NAME
    DEFAULT_NUM_VECTORS = 10
    DEFAULT_DIMENSION = 10
    DEFAULT_RANDOM_STRING_LENGTH = 8
    DEFAULT_ID_FIELD_NAME = "_id"

    def setUp(self) -> None:
        self.data_set_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.data_set_dir)

    def test_invalid_nesting_scheme(self):
        # Test with 0 "." in the vector field, with 2 "." in the vector field, and with a different separator.
        invalid_nesting_schemes = ["a", "a.b.c", "a.b.c.d"]
        for nesting_scheme in invalid_nesting_schemes:
            with self.subTest(nesting_scheme=nesting_scheme):
                bulk_param_source = BulkVectorsFromDataSetParamSource(
                    workload.Workload(name="unit-test"),
                    {
                        "index": self.DEFAULT_INDEX_NAME,
                        "field": nesting_scheme,
                        "data_set_format": self.DEFAULT_TYPE,
                        "data_set_path": "path",
                        "bulk_size": 10,
                        "id-field-name": self.DEFAULT_ID_FIELD_NAME,
                    },
                )
                with self.assertRaises(ValueError):
                    bulk_param_source.get_split_fields()

    def _test_params_default(
        self, bulk_size, data_set_path, parent_data_set_path, num_vectors
    ):
        test_param_source_params = {
            "index": self.DEFAULT_INDEX_NAME,
            "field": self.DEFAULT_VECTOR_FIELD_NAME,
            "data_set_format": self.DEFAULT_TYPE,
            "data_set_path": data_set_path,
            "bulk_size": bulk_size,
            "id-field-name": self.DEFAULT_ID_FIELD_NAME,
        }
        bulk_param_source = BulkVectorsFromDataSetParamSource(
            workload.Workload(name="unit-test"), test_param_source_params
        )
        bulk_param_source.parent_data_set_path = parent_data_set_path
        bulk_param_source_partition = bulk_param_source.partition(0, 1)
        # Check each payload returned
        vectors_consumed = 0
        while vectors_consumed < num_vectors:
            expected_num_vectors = min(num_vectors - vectors_consumed, bulk_size)
            actual_params = bulk_param_source_partition.params()
            expected_num_docs = len(actual_params["body"]) // 2

            self._check_params_nested(
                actual_params,
                self.DEFAULT_INDEX_NAME,
                self.DEFAULT_VECTOR_FIELD_NAME,
                self.DEFAULT_DIMENSION,
                expected_num_vectors,
                expected_num_docs,
                self.DEFAULT_ID_FIELD_NAME,
            )
            vectors_consumed += expected_num_vectors

        # Assert last call creates stop iteration
        with self.assertRaises(StopIteration):
            bulk_param_source_partition.params()

    def test_params_default(self):

        bulk_sizes = [1, 3, 4, 10, 50]

        num_vectors = 49
        # bulk_size = 10
        data_set_path = create_data_set(
            num_vectors,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.INDEX,
            self.data_set_dir,
        )
        parent_data_set_path = create_parent_data_set(
            num_vectors,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.PARENTS,
            self.data_set_dir,
        )

        for bulk_size in bulk_sizes:
            with self.subTest(bulk_size=bulk_size):
                self._test_params_default(
                    bulk_size, data_set_path, parent_data_set_path, num_vectors
                )

    def test_params_custom(self):
        num_vectors = 49
        bulk_size = 15
        data_set_path = create_data_set(
            num_vectors,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.INDEX,
            self.data_set_dir,
        )

        parent_data_set_path = create_parent_data_set(
            num_vectors,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.PARENTS,
            self.data_set_dir,
        )

        test_param_source_params = {
            "index": self.DEFAULT_INDEX_NAME,
            "field": self.DEFAULT_VECTOR_FIELD_NAME,
            "data_set_format": self.DEFAULT_TYPE,
            "data_set_path": data_set_path,
            "parents_data_set_path": parent_data_set_path,
            "bulk_size": bulk_size,
            "id-field-name": "id",
        }

        # todo is it weird with the parent data set path?
        bulk_param_source = BulkVectorsFromDataSetParamSource(
            workload.Workload(name="unit-test"), test_param_source_params
        )
        bulk_param_source.parent_data_set_path = parent_data_set_path
        bulk_param_source_partition = bulk_param_source.partition(0, 1)
        # Check each payload returned
        vectors_consumed = 0
        while vectors_consumed < num_vectors:
            # expected_num_vectors = 10, 30, 10, 9 (15, 15, 15, 4)
            expected_num_vectors = min(num_vectors - vectors_consumed, bulk_size)
            # expected_num_documents = min()
            actual_params = bulk_param_source_partition.params()
            expected_num_docs = len(actual_params["body"]) // 2
            self._check_params_nested(
                actual_params,
                self.DEFAULT_INDEX_NAME,
                self.DEFAULT_VECTOR_FIELD_NAME,
                self.DEFAULT_DIMENSION,
                expected_num_vectors,
                expected_num_docs,
                "id",
            )
            vectors_consumed += expected_num_vectors

        # Assert last call creates stop iteration
        with self.assertRaises(StopIteration):
            bulk_param_source_partition.params()

    def test_build_vector_search_query_body(self):
        k = 12
        data_set_path = create_data_set(
            self.DEFAULT_NUM_VECTORS,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.QUERY,
            self.data_set_dir
        )
        create_data_set(
            self.DEFAULT_NUM_VECTORS,
            self.DEFAULT_DIMENSION,
            self.DEFAULT_TYPE,
            Context.NEIGHBORS,
            self.data_set_dir,
            data_set_path
        )

        # Create a QueryVectorsFromDataSetParamSource with relevant params
        test_param_source_params = {
            "field": self.DEFAULT_VECTOR_FIELD_NAME,
            "data_set_format": self.DEFAULT_TYPE,
            "data_set_path": data_set_path,
            "k": k
        }
        query_param_source = VectorSearchPartitionParamSource(
            workload.Workload(name="unit-test"),
            test_param_source_params, {
                "index": self.DEFAULT_INDEX_NAME,
                "request-params": {},
            }
        )
        query_param_source_partition = query_param_source.partition(0, 1)

        # Check each
        for _ in range(DEFAULT_NUM_VECTORS):
            self._check_query_params(
                query_param_source_partition.params(),
                self.DEFAULT_VECTOR_FIELD_NAME,
                self.DEFAULT_DIMENSION,
                k,
            )

        # Assert last call creates stop iteration
        with self.assertRaises(StopIteration):
            query_param_source_partition.params()

    def _check_query_params(
            self,
            actual_params: dict,
            expected_field: str,
            expected_dimension: int,
            expected_k: int,
            expected_size=None,
            expected_filter=None,
    ):
        body = actual_params.get("body")
        self.assertIsInstance(body, dict)
        query = body.get("query")
        self.assertIsInstance(query, dict)
        nested = query.get("nested")
        self.assertIsInstance(nested, dict)

        outer, _inner = expected_field.split(".")

        path = nested.get("path")
        self.assertEqual(path, outer)

        query_knn = nested.get("query").get("knn")

        field = query_knn.get(expected_field)
        self.assertIsInstance(field, dict)
        vector = field.get("vector")
        self.assertIsInstance(vector, np.ndarray)
        self.assertEqual(len(list(vector)), expected_dimension)
        k = field.get("k")
        self.assertEqual(k, expected_k)
        neighbor = actual_params.get("neighbors")
        self.assertIsInstance(neighbor, list)
        self.assertEqual(len(neighbor), expected_dimension)
        size = body.get("size")
        self.assertEqual(size, expected_size if expected_size else expected_k)
        self.assertEqual(field.get("filter"), expected_filter)

    def _check_params_nested(
        self,
        actual_params: dict,
        expected_index: str,
        expected_vector_field: str,
        expected_dimension: int,
        _expected_num_vectors_in_payload: int,
        expected_num_docs_in_payload: int,
        expected_id_field: str,
    ):
        size = actual_params.get("size")
        self.assertEqual(size, expected_num_docs_in_payload)
        body = actual_params.get("body")
        self.assertIsInstance(body, list)
        self.assertEqual(len(body) // 2, expected_num_docs_in_payload)

        # Bulk payload has 2 parts: first one is the header and the second one
        # is the body. The header will have the index name and the body will
        # have the vector
        for header, req_body in zip(*[iter(body)] * 2):
            index = header.get("index")
            self.assertIsInstance(index, dict)

            index_name = index.get("_index")
            self.assertEqual(index_name, expected_index)
            # here, need to iterate over all of the nested fields.
            outer, inner = expected_vector_field.split(".")
            vector_list = req_body.get(outer)
            self.assertIsInstance(vector_list, list)
            for vec in vector_list:
                actual_vec = vec.get(inner)
                self.assertIsInstance(actual_vec, list)

                self.assertEqual(len(actual_vec), expected_dimension)

            if expected_id_field in index:
                self.assertEqual(self.DEFAULT_ID_FIELD_NAME, expected_id_field)
                self.assertFalse(expected_id_field in req_body)
                continue
            self.assertTrue(expected_id_field in req_body)

    def test_nested_vector_query_body(self):
        # assert that _build_vector_search_query_body returns the correct thing.
        pass
