import logging
from typing import List

from dbcat.catalog import Catalog, CatColumn, CatTable

from data_lineage.parser.table_visitor import (
    ColumnRefVisitor,
    RangeVarVisitor,
    TableVisitor,
)
from data_lineage.parser.visitor import Visitor


class DmlVisitor(Visitor):
    def __init__(self, name: str):
        self._name = name
        self._target_table = None
        self._target_columns: List[CatColumn] = []
        self._source_tables: List[CatTable] = []
        self._source_columns: List[CatColumn] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def target_table(self) -> CatTable:
        return self._target_table

    @property
    def target_columns(self) -> List[CatColumn]:
        return self._target_columns

    @property
    def source_tables(self) -> List[CatTable]:
        return self._source_tables

    @property
    def source_columns(self) -> List[CatColumn]:
        return self._source_columns

    def visit_range_var(self, node):
        self._target_table = node

    def visit_res_target(self, node):
        self._target_columns.append(node.name.value)

    def resolve(self):
        target_table_visitor = RangeVarVisitor()
        target_table_visitor.visit(self._target_table)

        self._target_table = target_table_visitor.fqdn

        bound_tables = []
        for table in self._source_tables:
            visitor = RangeVarVisitor()
            visitor.visit(table)
            bound_tables.append(visitor.fqdn)

        self._source_tables = bound_tables

        bound_cols = []
        for column in self._source_columns:
            column_ref_visitor = ColumnRefVisitor()
            column_ref_visitor.visit(column)
            bound_cols.append(column_ref_visitor.name[0])

        self._source_columns = bound_cols

    def bind(self, catalog: Catalog):  # noqa: C901
        target_table_visitor = RangeVarVisitor()
        target_table_visitor.visit(self._target_table)

        logging.debug("Searching for: {}".format(target_table_visitor.search_string))
        self._target_table = catalog.search_table(**target_table_visitor.search_string)
        logging.debug("Bound target table: {}".format(self._target_table))

        if len(self._target_columns) == 0:
            self._target_columns = catalog.get_columns_for_table(self._target_table)
            logging.debug("Bound all columns in {}".format(self._target_table))
        else:
            bound_cols = catalog.get_columns_for_table(
                self._target_table, column_names=self._target_columns
            )
            # Handle error case
            if len(bound_cols) != len(self._target_columns):
                for column in self._target_columns:
                    found = False
                    for bound in bound_cols:
                        if column == bound.name:
                            found = True
                            break

                    if not found:
                        raise RuntimeError("'{}' column is not found".format(column))

            self._target_columns = bound_cols
            logging.debug("Bound {} target columns".format(len(bound_cols)))

        alias_map = {}
        bound_tables = set()
        for table in self._source_tables:
            visitor = RangeVarVisitor()
            visitor.visit(table)
            if visitor.alias is not None:
                alias_map[visitor.alias] = visitor.search_string

            logging.debug("Searching for: {}".format(visitor.search_string))

            candidate_table = catalog.search_table(**visitor.search_string)
            logging.debug("Bound source table: {}".format(candidate_table))
            bound_tables.add(candidate_table)

        self._source_tables = list(bound_tables)
        bound_cols = []
        for column in self._source_columns:
            column_ref_visitor = ColumnRefVisitor()
            column_ref_visitor.visit(column)
            if column_ref_visitor.is_qualified:
                if column_ref_visitor.name[0] in alias_map:
                    table_name = alias_map[column_ref_visitor.name[0]]
                else:
                    table_name = {"table_like": column_ref_visitor.name[0]}

                logging.debug("Searching for: {}".format(table_name))
                candidate_table = catalog.search_table(**table_name)

                if column_ref_visitor.is_a_star:
                    bound_cols = bound_cols + catalog.get_columns_for_table(
                        candidate_table
                    )
                    logging.debug(
                        "Bound all source columns in {}".format(candidate_table)
                    )
                else:
                    bound = catalog.get_columns_for_table(
                        table=candidate_table, column_names=[column_ref_visitor.name[1]]
                    )
                    if len(bound) == 0:
                        raise RuntimeError("{} not found in table".format(column))
                    elif len(bound) > 1:
                        raise RuntimeError(
                            "Ambiguous column name. Multiple matches found"
                        )
                    logging.debug("Bound source column: {}".format(bound[0]))
                    bound_cols.append(bound[0])
            else:
                if column_ref_visitor.is_a_star:
                    for table in self._source_tables:
                        bound_cols = bound_cols + catalog.get_columns_for_table(table)
                        logging.debug("Bound all source columns in {}".format(table))
                else:
                    found = False
                    for table in self._source_tables:
                        bound = catalog.get_columns_for_table(
                            table=table, column_names=[column_ref_visitor.name[0]]
                        )
                        if len(bound) == 1:
                            if not found:
                                logging.debug(
                                    "Bound source column: {}".format(bound[0])
                                )
                                bound_cols.append(bound[0])
                                found = True
                            else:
                                raise RuntimeError(
                                    "Ambiguous column name {}. Multiple matches found".format(
                                        column
                                    )
                                )

                    if not found:
                        raise RuntimeError("{} not found in any tables.".format(column))

        self._source_columns = bound_cols


class SelectSourceVisitor(DmlVisitor):
    def __init__(self, name):
        super(SelectSourceVisitor, self).__init__(name)
        self._with_aliases = {}

    def visit_select_stmt(self, node):
        table_visitor = TableVisitor()
        table_visitor.visit(node)
        self._source_tables = table_visitor.sources
        self._source_columns = table_visitor.columns

    def visit_common_table_expr(self, node):
        with_alias = node.ctename.value
        table_visitor = TableVisitor()
        table_visitor.visit(node.ctequery)

        self._with_aliases[with_alias] = {
            "tables": table_visitor.sources,
            "columns": table_visitor.columns,
        }

    def resolve(self):
        super(SelectSourceVisitor, self).resolve()
        if self._with_aliases:
            # Resolve all the WITH expressions
            for key in self._with_aliases:
                bound_tables = []
                for table in self._with_aliases[key]["tables"]:
                    visitor = RangeVarVisitor()
                    visitor.visit(table)
                    bound_tables.append(visitor.fqdn)

                self._with_aliases[key]["bound_tables"] = bound_tables

                bound_cols = []
                for column in self._with_aliases[key]["columns"]:
                    column_ref_visitor = ColumnRefVisitor()
                    column_ref_visitor.visit(column)
                    bound_cols.append(column_ref_visitor.name[0])

                self._with_aliases[key]["bound_columns"] = bound_cols

            # Replace the bound tables with those bound from with clause
            replace_target_tables = []
            for table in self._source_tables:
                replaced = False
                for key in self._with_aliases.keys():
                    if table == (None, key):
                        replace_target_tables = (
                            replace_target_tables
                            + self._with_aliases[key]["bound_tables"]
                        )
                        replaced = True

                if not replaced:
                    replace_target_tables.append(table)
            self._source_tables = replace_target_tables


class SelectIntoVisitor(DmlVisitor):
    def __init__(self, name):
        super(SelectIntoVisitor, self).__init__(name)

    def visit_select_stmt(self, node):
        super(SelectIntoVisitor, self).visit(node.intoClause)
        table_visitor = TableVisitor()
        table_visitor.visit(node)
        self._source_tables = table_visitor.sources
        self._source_columns = table_visitor.columns


class CopyFromVisitor(DmlVisitor):
    def __init__(self, name):
        super(CopyFromVisitor, self).__init__(name)

    def visit_copy_stmt(self, node):
        if node.is_from:
            super(CopyFromVisitor, self).visit_copy_stmt(node)
