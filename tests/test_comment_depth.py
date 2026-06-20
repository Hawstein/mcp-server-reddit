"""Regression tests for get_post_content propagating comment_depth.

The bug: get_post_content accepted a comment_depth parameter and the
dispatcher passed it through, but inside get_post_content the call to
get_post_comments dropped the argument. get_post_comments then called
_build_comment_tree(node) with no depth argument, defaulting to 3 every
time. The user-supplied comment_depth had no effect, silently
truncating threads deeper than 3 levels.
"""

from unittest.mock import MagicMock, patch

from mcp_server_reddit.server import RedditServer, Comment


def _make_comment_node(comment_id, body, children=None):
    """Build a mock comment-tree node with the shape redditwarp returns."""
    node = MagicMock()
    node.value.id36 = comment_id
    node.value.author_display_name = 'tester'
    node.value.body = body
    node.value.score = 1
    node.children = children or []
    return node


def _make_server():
    with patch.object(RedditServer, '__init__', lambda self: None):
        server = RedditServer()
        server.client = MagicMock()
        return server


def _build_5_level_chain():
    """Return the root of a 5-level-deep linear comment chain."""
    leaf = _make_comment_node('c5', 'level 5')
    c4 = _make_comment_node('c4', 'level 4', [leaf])
    c3 = _make_comment_node('c3', 'level 3', [c4])
    c2 = _make_comment_node('c2', 'level 2', [c3])
    c1 = _make_comment_node('c1', 'level 1', [c2])
    return c1


def _depth_of(comment: Comment) -> int:
    depth = 1
    node = comment
    while node.replies:
        depth += 1
        node = node.replies[0]
    return depth


def test_get_post_comments_default_depth_is_3():
    server = _make_server()
    tree = MagicMock()
    tree.children = [_build_5_level_chain()]
    server.client.p.comment_tree.fetch.return_value = tree

    result = server.get_post_comments('postid')

    assert len(result) == 1
    assert _depth_of(result[0]) == 3


def test_get_post_comments_respects_depth_arg():
    server = _make_server()
    tree = MagicMock()
    tree.children = [_build_5_level_chain()]
    server.client.p.comment_tree.fetch.return_value = tree

    result = server.get_post_comments('postid', limit=10, depth=5)

    assert len(result) == 1
    assert _depth_of(result[0]) == 5


def test_get_post_content_propagates_comment_depth():
    """The bug site: depth was dropped on the way to get_post_comments."""
    server = _make_server()
    with patch.object(server, '_build_post'), \
         patch.object(server, 'get_post_comments', return_value=[]) as mock_get_comments, \
         patch('mcp_server_reddit.server.PostDetail'):
        server.get_post_content('postid', comment_limit=7, comment_depth=5)

    mock_get_comments.assert_called_once_with('postid', 7, 5)
