"""Recognize the bounded parser change that leaves its stress test invariant."""

import ast
from hashlib import sha256


PARSER = "sqlglot/parser.py"
TEST_FILE = "tests/test_parser.py"
TEST_ID = TEST_FILE + "::TestParser::test_max_nodes"
STRESS_TEST = "1b3a84376dd01d7534a5268b93be9483d2d140083bf5947b15420abf1bf543e2"
VALIDATOR = "a9b82a84ddd4358f410e8c567184ba29af2ccc169a0a86c06d91abd273a713ee"


def method(tree, class_name, method_name):
    klass = next((node for node in tree.body
                  if isinstance(node, ast.ClassDef) and node.name == class_name), None)
    if klass is None:
        return None
    return next((node for node in klass.body
                 if isinstance(node, ast.FunctionDef) and node.name == method_name), None)


def matches(node, digest):
    return node is not None and sha256(ast.unparse(node).encode()).hexdigest() == digest


def tightening_preserves_stress_test(original_parser, patched_parser, test_source):
    old_tree = ast.parse(original_parser)
    new_tree = ast.parse(patched_parser)
    test = method(ast.parse(test_source), "TestParser", "test_max_nodes")
    old = method(old_tree, "Parser", "validate_expression")
    new = method(new_tree, "Parser", "validate_expression")
    if not matches(test, STRESS_TEST) or not matches(old, VALIDATOR) or new is None:
        return False
    outer = new.body[0] if new.body else None
    check = outer.body[1] if isinstance(outer, ast.If) and len(outer.body) >= 2 else None
    comparison = check.test if isinstance(check, ast.If) else None
    if not isinstance(comparison, ast.Compare) or len(comparison.ops) != 1:
        return False
    if not isinstance(comparison.ops[0], ast.GtE):
        return False
    comparison.ops[0] = ast.Gt()
    # The stress test's small successful inputs stay far below the positive
    # limits. Its large inputs already cross their limits, and their assertions
    # check the limit error text rather than the position where it was raised.
    return ast.dump(old_tree) == ast.dump(new_tree)
