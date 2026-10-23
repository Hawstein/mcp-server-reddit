import asyncio
from enum import Enum
import json
import logging
import re
from typing import Any, Sequence
import redditwarp.SYNC
import redditwarp.models.subreddit
from redditwarp.http.exceptions import StatusCodeException
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
from mcp.shared.exceptions import McpError
from pydantic import BaseModel

# Workaround for redditwarp bug: Reddit sometimes omits 'active_user_count'
# from subreddit JSON, causing KeyError in Subreddit.__init__.
if not getattr(redditwarp.models.subreddit.Subreddit.__init__, '_patched', False):
    _original_subreddit_init = redditwarp.models.subreddit.Subreddit.__init__

    def _patched_subreddit_init(self, d, *args, **kwargs):
        if 'active_user_count' not in d:
            d = dict(d)
            d['active_user_count'] = None
        _original_subreddit_init(self, d, *args, **kwargs)

    _patched_subreddit_init._patched = True
    redditwarp.models.subreddit.Subreddit.__init__ = _patched_subreddit_init


logger = logging.getLogger(__name__)

# Reddit's own naming rules. Anything else is rejected before it reaches redditwarp,
# which interpolates names and IDs into URL paths unencoded (so '../' or '?' would
# otherwise steer the request to a different endpoint).
SUBREDDIT_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_]{1,20}$')
POST_ID_RE = re.compile(r'^[a-z0-9]{1,13}$')
TOP_TIME_FILTERS = ("", "hour", "day", "week", "month", "year", "all")


class InvalidArgument(ValueError):
    """A tool argument failed validation; its message is safe to show the caller."""


def _require_subreddit_name(arguments: dict) -> str:
    value = arguments.get("subreddit_name")
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument("Missing required argument: subreddit_name")
    name = value.strip().removeprefix("/").removeprefix("r/")
    if not SUBREDDIT_NAME_RE.match(name):
        raise InvalidArgument(f"Invalid subreddit name: {value!r}")
    return name


def _require_post_id(arguments: dict) -> str:
    value = arguments.get("post_id")
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument("Missing required argument: post_id")
    post_id = value.strip().lower().removeprefix("t3_")
    if not POST_ID_RE.match(post_id):
        raise InvalidArgument(f"Invalid post ID: {value!r}")
    return post_id


def _int_arg(arguments: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    # The JSON schema bounds aren't enforced by every MCP SDK version, and redditwarp
    # treats a None amount as "fetch everything", so check them here.
    value = arguments.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgument(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise InvalidArgument(f"{key} must be between {minimum} and {maximum}")
    return value


class PostType(str, Enum):
    LINK = "link"
    TEXT = "text"
    GALLERY = "gallery"
    UNKNOWN = "unknown"


class RedditTools(str, Enum):
    GET_FRONTPAGE_POSTS = "get_frontpage_posts"
    GET_SUBREDDIT_INFO = "get_subreddit_info"
    GET_SUBREDDIT_HOT_POSTS = "get_subreddit_hot_posts"
    GET_SUBREDDIT_NEW_POSTS = "get_subreddit_new_posts"
    GET_SUBREDDIT_TOP_POSTS = "get_subreddit_top_posts"
    GET_SUBREDDIT_RISING_POSTS = "get_subreddit_rising_posts"
    GET_POST_CONTENT = "get_post_content"
    GET_POST_COMMENTS = "get_post_comments"


class SubredditInfo(BaseModel):
    name: str
    subscriber_count: int
    description: str | None


class Post(BaseModel):
    id: str
    title: str
    author: str
    score: int
    subreddit: str
    url: str
    created_at: str
    comment_count: int
    post_type: PostType
    content: str | None


class Comment(BaseModel):
    id: str
    author: str
    body: str
    score: int
    replies: list['Comment'] = []


class Moderator(BaseModel):
    name: str


class PostDetail(BaseModel):
    post: Post
    comments: list[Comment]


class RedditServer:
    def __init__(self):
        self.client = redditwarp.SYNC.Client()

    def _get_post_type(self, submission) -> PostType:
        """Helper method to determine post type"""
        if isinstance(submission, redditwarp.models.submission_SYNC.LinkPost):
            return PostType.LINK
        elif isinstance(submission, redditwarp.models.submission_SYNC.TextPost):
            return PostType.TEXT
        elif isinstance(submission, redditwarp.models.submission_SYNC.GalleryPost):
            return PostType.GALLERY
        return PostType.UNKNOWN

    # The type can actually be determined by submission.post_hint
    # - self for text
    # - image for image
    # - hosted:video for video
    def _get_post_content(self, submission) -> str | None:
        """Helper method to extract post content based on type"""
        if isinstance(submission, redditwarp.models.submission_SYNC.LinkPost):
            return submission.link
        elif isinstance(submission, redditwarp.models.submission_SYNC.TextPost):
            return submission.body
        elif isinstance(submission, redditwarp.models.submission_SYNC.GalleryPost):
            return str(submission.gallery_link)
        return None

    def _build_post(self, submission) -> Post:
        """Helper method to build Post object from submission"""
        return Post(
            id=submission.id36,
            title=submission.title,
            author=submission.author_display_name or '[deleted]',
            score=submission.score,
            subreddit=submission.subreddit.name,
            url=submission.permalink,
            created_at=submission.created_at.astimezone().isoformat(),
            comment_count=submission.comment_count,
            post_type=self._get_post_type(submission),
            content=self._get_post_content(submission)
        )

    def get_frontpage_posts(self, limit: int = 10) -> list[Post]:
        """Get hot posts from Reddit frontpage"""
        posts = []
        for subm in self.client.p.front.pull.hot(limit):
            posts.append(self._build_post(subm))
        return posts

    def get_subreddit_info(self, subreddit_name: str) -> SubredditInfo:
        """Get information about a subreddit"""
        subr = self.client.p.subreddit.fetch_by_name(subreddit_name)
        return SubredditInfo(
            name=subr.name,
            subscriber_count=subr.subscriber_count,
            description=subr.public_description
        )

    def _build_comment_tree(self, node, depth: int) -> Comment | None:
        """Helper method to recursively build comment tree"""
        if depth <= 0 or not node:
            return None

        comment = node.value
        replies = []
        for child in node.children:
            child_comment = self._build_comment_tree(child, depth - 1)
            if child_comment:
                replies.append(child_comment)

        return Comment(
            id=comment.id36,
            author=comment.author_display_name or '[deleted]',
            body=comment.body,
            score=comment.score,
            replies=replies
        )

    def get_subreddit_hot_posts(self, subreddit_name: str, limit: int = 10) -> list[Post]:
        """Get hot posts from a specific subreddit"""
        posts = []
        for subm in self.client.p.subreddit.pull.hot(subreddit_name, limit):
            posts.append(self._build_post(subm))
        return posts

    def get_subreddit_new_posts(self, subreddit_name: str, limit: int = 10) -> list[Post]:
        """Get new posts from a specific subreddit"""
        posts = []
        for subm in self.client.p.subreddit.pull.new(subreddit_name, limit):
            posts.append(self._build_post(subm))
        return posts

    def get_subreddit_top_posts(self, subreddit_name: str, limit: int = 10, time: str = '') -> list[Post]:
        """Get top posts from a specific subreddit"""
        posts = []
        for subm in self.client.p.subreddit.pull.top(subreddit_name, limit, time=time):
            posts.append(self._build_post(subm))
        return posts

    def get_subreddit_rising_posts(self, subreddit_name: str, limit: int = 10) -> list[Post]:
        """Get rising posts from a specific subreddit"""
        posts = []
        for subm in self.client.p.subreddit.pull.rising(subreddit_name, limit):
            posts.append(self._build_post(subm))
        return posts

    def get_post_content(self, post_id: str, comment_limit: int = 10, comment_depth: int = 3) -> PostDetail:
        """Get detailed content of a specific post including comments"""
        # The comment tree response already includes the submission, so one request covers both.
        tree_node = self._fetch_comment_tree(post_id, comment_limit, comment_depth)
        post = self._build_post(tree_node.value)
        comments = self._build_comments(tree_node, comment_depth)
        return PostDetail(post=post, comments=comments)

    def get_post_comments(self, post_id: str, limit: int = 10, depth: int = 3) -> list[Comment]:
        """Get comments from a post"""
        tree_node = self._fetch_comment_tree(post_id, limit, depth)
        return self._build_comments(tree_node, depth)

    def _fetch_comment_tree(self, post_id: str, limit: int, depth: int):
        return self.client.p.comment_tree.fetch(post_id, sort='top', limit=limit, depth=depth)

    def _build_comments(self, tree_node, depth: int) -> list[Comment]:
        comments = []
        for node in tree_node.children:
            comment = self._build_comment_tree(node, depth)
            if comment:
                comments.append(comment)
        return comments


async def serve() -> None:
    server = Server("mcp-reddit")
    reddit_server = RedditServer()
    # redditwarp's SYNC client blocks, so run it off the event loop. The lock keeps
    # calls on the shared client sequential, as they were before.
    reddit_lock = asyncio.Lock()

    async def run_blocking(func, *args: Any) -> Any:
        async with reddit_lock:
            return await asyncio.to_thread(func, *args)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available Reddit tools."""
        return [
            Tool(
                name=RedditTools.GET_FRONTPAGE_POSTS.value,
                description="Get hot posts from Reddit frontpage",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Number of posts to return (default: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100
                        }
                    }
                }
            ),
            Tool(
                name=RedditTools.GET_SUBREDDIT_INFO.value,
                description="Get information about a subreddit",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "subreddit_name": {
                            "type": "string",
                            "description": "Name of the subreddit (e.g. 'Python', 'news')",
                        }
                    },
                    "required": ["subreddit_name"]
                }
            ),
            Tool(
                name=RedditTools.GET_SUBREDDIT_HOT_POSTS.value,
                description="Get hot posts from a specific subreddit",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "subreddit_name": {
                            "type": "string",
                            "description": "Name of the subreddit (e.g. 'Python', 'news')",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of posts to return (default: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100
                        }
                    },
                    "required": ["subreddit_name"]
                }
            ),
            Tool(
                name=RedditTools.GET_SUBREDDIT_NEW_POSTS.value,
                description="Get new posts from a specific subreddit",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "subreddit_name": {
                            "type": "string",
                            "description": "Name of the subreddit (e.g. 'Python', 'news')",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of posts to return (default: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100
                        }
                    },
                    "required": ["subreddit_name"]
                }
            ),
            Tool(
                name=RedditTools.GET_SUBREDDIT_TOP_POSTS.value,
                description="Get top posts from a specific subreddit",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "subreddit_name": {
                            "type": "string",
                            "description": "Name of the subreddit (e.g. 'Python', 'news')",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of posts to return (default: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100
                        },
                        "time": {
                            "type": "string",
                            "description": "Time filter for top posts (e.g. 'hour', 'day', 'week', 'month', 'year', 'all')",
                            "default": "",
                            "enum": ["", "hour", "day", "week", "month", "year", "all"]
                        }
                    },
                    "required": ["subreddit_name"]
                }
            ),
            Tool(
                name=RedditTools.GET_SUBREDDIT_RISING_POSTS.value,
                description="Get rising posts from a specific subreddit",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "subreddit_name": {
                            "type": "string",
                            "description": "Name of the subreddit (e.g. 'Python', 'news')",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of posts to return (default: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100
                        }
                    },
                    "required": ["subreddit_name"]
                }
            ),
            Tool(
                name=RedditTools.GET_POST_CONTENT.value,
                description="Get detailed content of a specific post",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "post_id": {
                            "type": "string",
                            "description": "ID of the post",
                        },
                        "comment_limit": {
                            "type": "integer",
                            "description": "Number of top-level comments to return (default: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100
                        },
                        "comment_depth": {
                            "type": "integer",
                            "description": "Maximum depth of comment tree (default: 3)",
                            "default": 3,
                            "minimum": 1,
                            "maximum": 10
                        }
                    },
                    "required": ["post_id"]
                }
            ),
            Tool(
                name=RedditTools.GET_POST_COMMENTS.value,
                description="Get comments from a post",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "post_id": {
                            "type": "string",
                            "description": "ID of the post",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of comments to return (default: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100
                        }
                    },
                    "required": ["post_id"]
                }
            ),
        ]

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict
    ) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
        """Handle tool calls for Reddit API."""
        arguments = arguments or {}
        try:
            match name:
                case RedditTools.GET_FRONTPAGE_POSTS.value:
                    limit = _int_arg(arguments, "limit", 10, 1, 100)
                    result = await run_blocking(reddit_server.get_frontpage_posts, limit)

                case RedditTools.GET_SUBREDDIT_INFO.value:
                    subreddit_name = _require_subreddit_name(arguments)
                    result = await run_blocking(reddit_server.get_subreddit_info, subreddit_name)

                case RedditTools.GET_SUBREDDIT_HOT_POSTS.value:
                    subreddit_name = _require_subreddit_name(arguments)
                    limit = _int_arg(arguments, "limit", 10, 1, 100)
                    result = await run_blocking(reddit_server.get_subreddit_hot_posts, subreddit_name, limit)

                case RedditTools.GET_SUBREDDIT_NEW_POSTS.value:
                    subreddit_name = _require_subreddit_name(arguments)
                    limit = _int_arg(arguments, "limit", 10, 1, 100)
                    result = await run_blocking(reddit_server.get_subreddit_new_posts, subreddit_name, limit)

                case RedditTools.GET_SUBREDDIT_TOP_POSTS.value:
                    subreddit_name = _require_subreddit_name(arguments)
                    limit = _int_arg(arguments, "limit", 10, 1, 100)
                    time = arguments.get("time") or ""
                    if time not in TOP_TIME_FILTERS:
                        raise InvalidArgument(f"time must be one of: {', '.join(t for t in TOP_TIME_FILTERS if t)}")
                    result = await run_blocking(reddit_server.get_subreddit_top_posts, subreddit_name, limit, time)

                case RedditTools.GET_SUBREDDIT_RISING_POSTS.value:
                    subreddit_name = _require_subreddit_name(arguments)
                    limit = _int_arg(arguments, "limit", 10, 1, 100)
                    result = await run_blocking(reddit_server.get_subreddit_rising_posts, subreddit_name, limit)

                case RedditTools.GET_POST_CONTENT.value:
                    post_id = _require_post_id(arguments)
                    comment_limit = _int_arg(arguments, "comment_limit", 10, 1, 100)
                    comment_depth = _int_arg(arguments, "comment_depth", 3, 1, 10)
                    result = await run_blocking(reddit_server.get_post_content, post_id, comment_limit, comment_depth)

                case RedditTools.GET_POST_COMMENTS.value:
                    post_id = _require_post_id(arguments)
                    limit = _int_arg(arguments, "limit", 10, 1, 100)
                    result = await run_blocking(reddit_server.get_post_comments, post_id, limit)

                case _:
                    raise InvalidArgument(f"Unknown tool: {name}")

            return [
                TextContent(type="text", text=json.dumps(result, default=lambda x: x.model_dump(), indent=2))
            ]

        except InvalidArgument as e:
            # Our own argument validation: the message is safe and useful to show as-is.
            raise ValueError(f"Error processing mcp-server-reddit query: {e}") from e
        except StatusCodeException as e:
            logger.exception("Reddit request failed for tool %s", name)
            raise ValueError(
                f"Error processing mcp-server-reddit query: Reddit returned HTTP {e.status_code}"
            ) from e
        except Exception as e:
            # Don't forward raw library errors (URLs, response fragments) to the model;
            # the full traceback goes to the server log instead.
            logger.exception("Unexpected error in tool %s", name)
            raise ValueError(
                f"Error processing mcp-server-reddit query: unexpected {type(e).__name__}"
            ) from e

    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options)
