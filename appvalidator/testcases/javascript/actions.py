import math
import re
import types

import spidermonkey
import instanceactions
import instanceproperties
from appvalidator.python.copy import deepcopy
from appvalidator.constants import (BUGZILLA_BUG, DESCRIPTION_TYPES,
                                    MAX_STR_SIZE)
from jstypes import *


NUMERIC_TYPES = (int, long, float, complex)

# None of these operations (or their augmented assignment counterparts) should
# be performed on non-numeric data. Any time we get non-numeric data for these
# guys, we just return window.NaN.
NUMERIC_OPERATORS = ("-", "*", "/", "%", "<<", ">>", ">>>", "|", "^", "&")
NUMERIC_OPERATORS += tuple("%s=" % op for op in NUMERIC_OPERATORS)


def get_NaN(traverser):
    # If we've cached the traverser's NaN instance, just use that.
    ncache = getattr(traverser, "NAN_CACHE", None)
    if ncache is not None:
        return ncache

    # Otherwise, we need to import GLOBAL_ENTITIES and build a raw copy.
    from predefinedentities import GLOBAL_ENTITIES
    ncache = traverser._build_global("NaN", GLOBAL_ENTITIES[u"NaN"])
    # Cache it so we don't need to do this again.
    traverser.NAN_CACHE = ncache
    return ncache


def _get_member_exp_property(traverser, node):
    """Return the string value of a member expression's property."""

    if node["property"]["type"] == "Identifier" and not node["computed"]:
        return unicode(node["property"]["name"])
    else:
        eval_exp = traverser.traverse_node(node["property"])
        return _get_as_str(eval_exp.get_literal_value())


def _expand_globals(traverser, node):
    """Expands a global object that has a lambda value."""
    
    if not isinstance(node, JSWrapper):
        print 'jsw', node, traverser.filename, traverser.line
        return JSWrapper(node, traverser=traverser)

    if node.is_global and callable(node.value.get("value")):

        result = node.value["value"](t=traverser)
        if isinstance(result, dict):
            output = traverser._build_global("--", result)
        elif isinstance(result, JSWrapper):
            output = result
        else:
            output = JSWrapper(result, traverser)

        return output

    return node


def trace_member(traverser, node, instantiate=False):
    "Traces a MemberExpression and returns the appropriate object"

    traverser._debug("TESTING>>%s" % node["type"])
    if node["type"] == "MemberExpression":
        # x.y or x[y]
        # x = base
        base = trace_member(traverser, node["object"], instantiate)
        base = _expand_globals(traverser, base)

        identifier = _get_member_exp_property(traverser, node)

        traverser._debug("MEMBER_EXP>>PROPERTY (%s)" % identifier)
        output = base.get(
            traverser=traverser, instantiate=instantiate, name=identifier)
        return output

    elif node["type"] == "Identifier":
        traverser._debug("MEMBER_EXP>>ROOT:IDENTIFIER (%s)" % node["name"])

        # If we're supposed to instantiate the object and it doesn't already
        # exist, instantitate the object.
        if instantiate and not traverser._is_defined(node["name"]):
            output = JSWrapper(JSObject(), traverser=traverser)
            traverser.contexts[0].set(node["name"], output)
        else:
            output = traverser._seek_variable(node["name"])

        return _expand_globals(traverser, output)
    else:
        traverser._debug("MEMBER_EXP>>ROOT:EXPRESSION")
        # It's an expression, so just try your damndest.
        return traverser.traverse_node(node)


def _function(traverser, node):
    "Prevents code duplication"

    def wrap(traverser, node):
        me = JSObject()

        traverser.function_collection.append([])

        # Replace the current context with a prototypeable JS object.
        traverser._pop_context()
        me.type_ = "default"  # Treat the function as a normal object.
        traverser._push_context(me)
        traverser._debug("THIS_PUSH")
        traverser.this_stack.append(me)  # Allow references to "this"

        # Declare parameters in the local scope
        params = []
        for param in node["params"]:
            if param["type"] == "Identifier":
                params.append(param["name"])
            elif param["type"] == "ArrayPattern":
                for element in param["elements"]:
                    # Array destructuring in function prototypes? LOL!
                    if element is None or element["type"] != "Identifier":
                        continue
                    params.append(element["name"])

        local_context = traverser._peek_context(1)
        for param in params:
            var = JSWrapper(lazy=True, traverser=traverser)

            # We can assume that the params are static because we don't care
            # about what calls the function. We want to know whether the
            # function solely returns static values. If so, it is a static
            # function.
            local_context.set(param, var)

        traverser.traverse_node(node["body"])

        # Since we need to manually manage the "this" stack, pop off that
        # context.
        traverser._debug("THIS_POP")
        traverser.this_stack.pop()

        # Call all of the function collection's members to traverse all of the
        # child functions.
        func_coll = traverser.function_collection.pop()
        for func in func_coll:
            func()

    # Put the function off for traversal at the end of the current block scope.
    traverser.function_collection[-1].append(lambda: wrap(traverser, node))

    return JSWrapper(traverser=traverser, callable_=True, dirty=True)


def _define_function(traverser, node):
    me = _function(traverser, node)
    traverser._peek_context(2).set(node["id"]["name"], me)
    return me


# Just make it a refernce to the function that it aliases.
_func_expr = _function


def _define_with(traverser, node):
    object_ = traverser.traverse_node(node["object"])
    if (isinstance(object_, JSWrapper) and
        isinstance(object_.value, JSObject)):
        traverser.contexts[-1] = object_.value
        traverser.contexts.append(JSContext("block"))


def _define_var(traverser, node):
    traverser._debug("VARIABLE_DECLARATION")
    traverser.debug_level += 1

    for declaration in node["declarations"]:

        # It could be deconstruction of variables :(
        if declaration["id"]["type"] == "ArrayPattern":

            vars = []
            for element in declaration["id"]["elements"]:
                # NOTE : Multi-level array destructuring sucks. Maybe implement
                # it someday if you're bored, but it's so rarely used and it's
                # so utterly complex, there's probably no need to ever code it
                # up.
                if element is None or element["type"] != "Identifier":
                    vars.append(None)
                    continue
                vars.append(element["name"])

            # The variables are not initialized
            if declaration["init"] is None:
                # Simple instantiation; no initialization
                for var in filter(None, vars):
                    traverser._declare_variable(var, None)

            # The variables are declared inline
            elif declaration["init"]["type"] == "ArrayPattern":
                # TODO : Test to make sure len(values) == len(vars)
                for value in declaration["init"]["elements"]:
                    if vars[0]:
                        traverser._declare_variable(
                            vars[0], JSWrapper(traverser.traverse_node(value),
                                               traverser=traverser))
                    vars = vars[1:]  # Pop off the first value

            # It's being assigned by a JSArray (presumably)
            elif declaration["init"]["type"] == "ArrayExpression":
                assigner = traverser.traverse_node(declaration["init"])
                for value in assigner.value.elements:
                    if vars[0]:
                        traverser._declare_variable(vars[0], value)
                    vars = vars[1:]

        elif declaration["id"]["type"] == "ObjectPattern":

            init = traverser.traverse_node(declaration["init"])

            def _proc_objpattern(init_obj, properties):
                for prop in properties:
                    # Get the name of the init obj's member
                    if prop["key"]["type"] == "Literal":
                        prop_name = prop["key"]["value"]
                    elif prop["key"]["type"] == "Identifier":
                        prop_name = prop["key"]["name"]
                    else:
                        continue

                    if prop["value"]["type"] == "Identifier":
                        traverser._declare_variable(
                            prop["value"]["name"],
                            init_obj.get(traverser, prop_name))
                    elif prop["value"]["type"] == "ObjectPattern":
                        _proc_objpattern(init_obj.get(traverser, prop_name),
                                         prop["value"]["properties"])

            if init is not None:
                _proc_objpattern(init_obj=init,
                                 properties=declaration["id"]["properties"])

        else:
            var_name = declaration["id"]["name"]
            traverser._debug("NAME>>%s" % var_name)

            var = traverser.traverse_node(declaration["init"])
            traverser._debug("VALUE>>%s" % (var.output()
                                            if var is not None
                                            else "None"))

            if not isinstance(var, JSWrapper):
                var = JSWrapper(value=var_value, traverser=traverser)
            var.const = node["kind"] == "const"
            traverser._declare_variable(var_name, var, type_=node["kind"])

    traverser.debug_level -= 1

    # The "Declarations" branch contains custom elements.
    return True


def _define_obj(traverser, node):
    "Creates a local context object"

    var = JSObject()
    for prop in node["properties"]:
        var_name = ""
        key = prop["key"]
        var_name = key["value" if key["type"] == "Literal" else "name"]
        var_value = traverser.traverse_node(prop["value"])
        var.set(var_name, var_value, traverser)

        # TODO: Observe "kind"

    if not isinstance(var, JSWrapper):
        return JSWrapper(var, lazy=True, traverser=traverser)
    var.lazy = True
    return var


def _define_array(traverser, node):
    """Instantiate an array object from the parse tree."""
    arr = JSArray()
    arr.elements = map(traverser.traverse_node, node["elements"])
    return arr


def _define_literal(traverser, node):
    """
    Convert a literal node in the parse tree to its corresponding
    interpreted value.
    """
    value = node["value"]
    if isinstance(value, dict):
        return JSWrapper(JSObject(), traverser=traverser, dirty=True)
    return JSWrapper(value if value is not None else JSLiteral(None),
                     traverser=traverser)


def _call_expression(traverser, node):
    args = node["arguments"]
    map(traverser.traverse_node, args)

    member = traverser.traverse_node(node["callee"])

    if member.is_global and callable(member.value.get("dangerous")):

        dangerous = member.value["dangerous"]
        t = traverser.traverse_node
        result = dangerous(a=args, t=t, e=traverser.err)
        if result and "name" in member.value:
            ## Generate a string representation of the params
            #params = u", ".join([_get_as_str(t(p).get_literal_value()) for
            #                     p in args])
            traverser.err.warning(
                err_id=("testcases_javascript_actions", "_call_expression",
                        "called_dangerous_global"),
                warning="`%s` called in potentially dangerous manner" %
                            member.value["name"],
                description=result if isinstance(result, DESCRIPTION_TYPES) else
                            "The global `%s` function was called using a set "
                            "of dangerous parameters. Calls of this nature "
                            "are deprecated." % member.value["name"],
                filename=traverser.filename,
                line=traverser.line,
                column=traverser.position,
                context=traverser.context)

    elif (node["callee"]["type"] == "MemberExpression" and
          node["callee"]["property"]["type"] == "Identifier"):

        # If we can identify the function being called on any member of any
        # instance, we can use that to either generate an output value or test
        # for additional conditions.
        identifier_name = node["callee"]["property"]["name"]
        if identifier_name in instanceactions.INSTANCE_DEFINITIONS:
            result = instanceactions.INSTANCE_DEFINITIONS[identifier_name](
                        args, traverser, node, wrapper=member)
            return result

    if member.is_global and "return" in member.value:
        return member.value["return"](wrapper=member, arguments=args,
                                      traverser=traverser)
    return JSWrapper(JSObject(), dirty=True, traverser=traverser)


def _expression(traverser, node):
    """
    This is a helper method that allows node definitions to point at
    `traverse_node` without needing a reference to a traverser.
    """
    return traverser.traverse_node(node["expression"])


def _get_this(traverser, node):
    "Returns the `this` object"
    if not traverser.this_stack:
        from predefinedentities import GLOBAL_ENTITIES
        return traverser._build_global("window", GLOBAL_ENTITIES[u"window"])
    return traverser.this_stack[-1]


def _new(traverser, node):
    "Returns a new copy of a node."

    # We don't actually process the arguments as part of the flow because of
    # the Angry T-Rex effect. For now, we just traverse them to ensure they
    # don't contain anything dangerous.
    args = node["arguments"]
    if isinstance(args, list):
        for arg in args:
            traverser.traverse_node(arg)
    else:
        traverser.traverse_node(args)

    elem = traverser.traverse_node(node["callee"])
    if not isinstance(elem, JSWrapper):
        elem = JSWrapper(elem, traverser=traverser)
    if elem.is_global:
        traverser._debug("Making overwritable")
        elem.value = deepcopy(elem.value)
        elem.value["overwritable"] = True
        elem.value["readonly"] = False
        if "new" in elem.value:
            elem = elem.value["new"](traverser, node, elem)
    return elem


def _ident(traverser, node):
    "Initiates an object lookup on the traverser based on an identifier token"

    name = node["name"]
    if traverser._is_defined(name):
        return traverser._seek_variable(name)

    return JSWrapper(JSObject(), traverser=traverser, dirty=True)


def _expr_assignment(traverser, node):
    """Evaluate an AssignmentExpression node."""

    traverser._debug("ASSIGNMENT_EXPRESSION")
    traverser.debug_level += 1

    traverser._debug("ASSIGNMENT>>PARSING RIGHT")
    right = traverser.traverse_node(node["right"])
    right = JSWrapper(right, traverser=traverser)

    # Treat direct assignment different than augmented assignment.
    if node["operator"] == "=":

        global_overwrite = False
        readonly_value = True

        node_left = node["left"]
        traverser._debug("ASSIGNMENT:DIRECT(%s)" % node_left["type"])

        if node_left["type"] == "Identifier":
            # Identifiers just need the ID name and a value to push.
            # Raise a global overwrite issue if the identifier is global.
            global_overwrite = traverser._is_global(node_left["name"])

            # Get the readonly attribute and store its value if is_global
            if global_overwrite:
                from predefinedentities import GLOBAL_ENTITIES
                global_dict = GLOBAL_ENTITIES[node_left["name"]]
                readonly_value = global_dict.get("readonly", True)

            traverser._declare_variable(node_left["name"], right, type_="glob")
            
        elif node_left["type"] == "MemberExpression":
            member_object = trace_member(traverser, node_left["object"],
                                         instantiate=True)
            global_overwrite = (member_object.is_global and
                                not member_object.value.get("overwritable"))
            member_property = _get_member_exp_property(traverser, node_left)
            traverser._debug("ASSIGNMENT:MEMBER_PROPERTY(%s)" % member_property)
            traverser._debug("ASSIGNMENT:GLOB_OV::%s" % global_overwrite)

            # Don't do the assignment if we're facing a global.
            if not global_overwrite:
                if member_object.value is None:
                    member_object.value = JSObject()

                if not member_object.is_global:
                    member_object.value.set(member_property, right, traverser)
                else:
                    # It's probably better to do nothing.
                    pass

            elif "value" in member_object.value:
                member_object_value = _expand_globals(
                    traverser, member_object).value
                if member_property in member_object_value["value"]:

                    # If it's a global and the actual member exists, test
                    # whether it can be safely overwritten.
                    member = member_object_value["value"][member_property]
                    if callable(member.get("value")):
                        member = member["value"](t=traverser)
                    readonly_value = member.get("readonly", True)

        traverser._debug("ASSIGNMENT:DIRECT:GLOB_OVERWRITE %s" %
                             global_overwrite)

        if callable(readonly_value):
            # The readonly attribute supports a lambda function that accepts
            readonly_value(t=traverser, r=right, rn=node["right"])

        return right

    lit_right = right.get_literal_value()

    traverser._debug("ASSIGNMENT>>PARSING LEFT")
    left = traverser.traverse_node(node["left"])
    traverser._debug("ASSIGNMENT>>DONE PARSING LEFT")
    traverser.debug_level -= 1

    if isinstance(left, JSWrapper):
        if left.dirty:
            return left

        lit_left = left.get_literal_value()
        token = node["operator"]

        # Don't perform an operation on None. Python freaks out
        if lit_left is None:
            lit_left = 0
        if lit_right is None:
            lit_right = 0

        # Give them default values so we have them in scope.
        gleft, gright = 0, 0

        # All of the assignment operators
        operators = {"=": lambda: right,
                     "+=": lambda: lit_left + lit_right,
                     "-=": lambda: gleft - gright,
                     "*=": lambda: gleft * gright,
                     "/=": lambda: 0 if gright == 0 else (gleft / gright),
                     "%=": lambda: 0 if gright == 0 else (gleft % gright),
                     "<<=": lambda: int(gleft) << int(gright),
                     ">>=": lambda: int(gleft) >> int(gright),
                     ">>>=": lambda: float(abs(int(gleft)) >> gright),
                     "|=": lambda: int(gleft) | int(gright),
                     "^=": lambda: int(gleft) ^ int(gright),
                     "&=": lambda: int(gleft) & int(gright)}

        # If we're modifying a non-numeric type with a numeric operator, return
        # NaN.
        if (not isinstance(lit_left, NUMERIC_TYPES) and
                token in NUMERIC_OPERATORS):
            left.set_value(get_NaN(traverser), traverser=traverser)
            return left

        # If either side of the assignment operator is a string, both sides
        # need to be casted to strings first.
        if (isinstance(lit_left, types.StringTypes) or
                isinstance(lit_right, types.StringTypes)):
            lit_left = _get_as_str(lit_left)
            lit_right = _get_as_str(lit_right)

        gleft, gright = _get_as_num(left), _get_as_num(right)

        traverser._debug("ASSIGNMENT>>OPERATION:%s" % token)
        if token not in operators:
            # We don't support that operator. (yet?)
            traverser._debug("ASSIGNMENT>>OPERATOR NOT FOUND", 1)
            return left
        elif token in ("<<=", ">>=", ">>>=") and gright < 0:
            # The user is doing weird bitshifting that will return 0 in JS but
            # not in Python.
            left.set_value(0, traverser=traverser)
            return left
        elif (token in ("<<=", ">>=", ">>>=", "|=", "^=", "&=") and
              (abs(gleft) == float('inf') or abs(gright) == float('inf'))):
            # Don't bother handling infinity for integer-converted operations.
            left.set_value(get_NaN(traverser), traverser=traverser)
            return left

        traverser._debug("ASSIGNMENT::L-value global? (%s)" %
                         ("Y" if left.is_global else "N"), 1)
        new_value = operators[token]()

        # Cap the length of analyzed strings.
        if (isinstance(new_value, types.StringTypes)
                and len(new_value) > MAX_STR_SIZE):
            new_value = new_value[:MAX_STR_SIZE]

        traverser._debug("ASSIGNMENT::New value >> %s" % new_value, 1)
        left.set_value(new_value, traverser=traverser)
        return left

    # Though it would otherwise be a syntax error, we say that 4=5 should
    # evaluate out to 5.
    return right


def _expr_binary(traverser, node):
    "Evaluates a BinaryExpression node."

    traverser.debug_level += 1

    # Select the proper operator.
    operator = node["operator"]
    traverser._debug("BIN_OPERATOR>>%s" % operator)

    # Traverse the left half of the binary expression.
    traverser._debug("BIN_EXP>>l-value")
    traverser.debug_level += 1

    if (node["left"]["type"] == "BinaryExpression" and
        "__traversal" not in node["left"]):
        # Process the left branch of the binary expression directly. This keeps
        # the recursion cap in line and speeds up processing of large chains
        # of binary expressions.
        left = _expr_binary(traverser, node["left"])
        node["left"]["__traversal"] = left
    else:
        left = traverser.traverse_node(node["left"])

    # Traverse the right half of the binary expression.
    traverser._debug("BIN_EXP>>r-value", -1)

    if (operator == "instanceof" and
            node["right"]["type"] == "Identifier" and
            node["right"]["name"] == "Function"):
        # We make an exception for instanceof's r-value if it's a dangerous
        # global, specifically Function.
        traverser.debug_level -= 1
        return JSWrapper(True, traverser=traverser)
    else:
        right = traverser.traverse_node(node["right"])
        traverser._debug("Is dirty? %r" % right.dirty, 1)

    traverser.debug_level -= 1

    # Dirty l or r values mean we can skip the expression. A dirty value
    # indicates that a lazy operation took place that introduced some
    # nondeterminacy.
    if operator != "+":
        # We don't want this to apply to concatenation.
        if left.dirty:
            return left
        elif right.dirty:
            return right

    # Binary expressions are only executed on literals.
    left_wrap = left
    left = left.get_literal_value()
    right_wrap = right
    right = right.get_literal_value()

    # Coerce the literals to numbers for numeric operations.
    gleft = _get_as_num(left)
    gright = _get_as_num(right)

    operators = {
        "==": lambda: left == right or gleft == gright,
        "!=": lambda: left != right,
        "===": lambda: left == right,  # Be flexible.
        "!==": lambda: type(left) != type(right) or left != right,
        ">": lambda: left > right,
        "<": lambda: left < right,
        "<=": lambda: left <= right,
        ">=": lambda: left >= right,
        "<<": lambda: int(gleft) << int(gright),
        ">>": lambda: int(gleft) >> int(gright),
        ">>>": lambda: float(abs(int(gleft)) >> int(gright)),
        "+": lambda: left + right,
        "-": lambda: gleft - gright,
        "*": lambda: gleft * gright,
        "/": lambda: 0 if gright == 0 else (gleft / gright),
        "%": lambda: 0 if gright == 0 else (gleft % gright),
        "in": lambda: right_wrap.contains(left),
        # TODO : implement instanceof
    }

    output = None
    if (operator in (">>", "<<", ">>>") and
            (left is None or right is None or gright < 0)):
        output = False
    elif operator in operators:
        # Concatenation can be silly, so always turn undefineds into empty
        # strings and if there are strings, make everything strings.
        if operator == "+":
            if left is None:
                left = ""
            if right is None:
                right = ""
            if (isinstance(left, types.StringTypes) or
                    isinstance(right, types.StringTypes)):
                left = _get_as_str(left)
                right = _get_as_str(right)

        # Don't even bother handling infinity if it's a numeric computation.
        if (operator in ("<<", ">>", ">>>") and
                (abs(gleft) == float('inf') or abs(gright) == float('inf'))):
            return get_NaN(traverser)

        output = operators[operator]()

        # Cap the length of analyzed strings.
        if (isinstance(output, types.StringTypes)
                and len(output) > MAX_STR_SIZE):
            output = output[:MAX_STR_SIZE]

    return JSWrapper(output, traverser=traverser)


def _expr_unary(traverser, node):
    """Evaluate a UnaryExpression node."""

    expr = traverser.traverse_node(node["argument"])
    expr_lit = expr.get_literal_value()
    expr_num = _get_as_num(expr_lit)

    operators = {"-": lambda: -1 * expr_num,
                 "+": lambda: expr_num,
                 "!": lambda: not expr_lit,
                 "~": lambda: -1 * (expr_num + 1),
                 "void": lambda: None,
                 "typeof": lambda: _expr_unary_typeof(expr),
                 "delete": lambda: None}  # We never want to empty the context
    if node["operator"] in operators:
        output = operators[node["operator"]]()
    else:
        output = None

    if not isinstance(output, JSWrapper):
        output = JSWrapper(output, traverser=traverser)
    return output


def _expr_unary_typeof(wrapper):
    """Evaluate the "typeof" value for a JSWrapper object."""
    if (wrapper.callable or
        (wrapper.is_global and "return" in wrapper.value and
         "value" not in wrapper.value)):
        return "function"

    value = wrapper.value
    if value is None:
        return "undefined"
    elif isinstance(value, JSLiteral):
        value = value.value
        if isinstance(value, bool):
            return "boolean"
        elif isinstance(value, (int, long, float)):
            return "number"
        elif isinstance(value, types.StringTypes):
            return "string"
    return "object"


def _get_as_num(value):
    """Return the JS numeric equivalent for a value."""
    if isinstance(value, JSWrapper):
        value = value.get_literal_value()

    if value is None:
        return 0

    try:
        if isinstance(value, types.StringTypes):
            if value.startswith("0x"):
                return int(value, 16)
            else:
                return float(value)
        elif isinstance(value, (int, float, long)):
            return value
        else:
            return int(value)
    except (ValueError, TypeError):
        return 0


def _get_as_str(value):
    """Return the JS string equivalent for a literal value."""
    if isinstance(value, JSWrapper):
        value = value.get_literal_value()

    if value is None:
        return ""

    if isinstance(value, bool):
        return u"true" if value else u"false"
    elif isinstance(value, (int, float, long)):
        if value == float('inf'):
            return u"Infinity"
        elif value == float('-inf'):
            return u"-Infinity"

        # Try to see if we can shave off some trailing significant figures.
        try:
            if int(value) == value:
                return unicode(int(value))
        except (ValueError, TypeError):
            pass
    return unicode(value)
