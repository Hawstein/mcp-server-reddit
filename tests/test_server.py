"""Tests for mcp-server-reddit server logic."""

from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from mcp_server_reddit.server import RedditServer, SubredditInfo


class TestGetSubredditInfo:
    """Test get_subreddit_info with normal and fallback paths."""

    def _make_server_with_mock_client(self):
        """Create a RedditServer with a mocked redditwarp client."""
        with patch.object(RedditServer, '__init__', lambda self: None):
            server = RedditServer()
            server.client = MagicMock()
            return server

    def test_normal_path(self):
        """Returns SubredditInfo when redditwarp model construction succeeds."""
        server = self._make_server_with_mock_client()

        mock_subr = MagicMock()
        mock_subr.name = 'python'
        mock_subr.subscriber_count = 1200000
        mock_subr.public_description = 'News about Python'
        server.client.p.subreddit.fetch_by_name.return_value = mock_subr

        result = server.get_subreddit_info('python')

        assert isinstance(result, SubredditInfo)
        assert result.name == 'python'
        assert result.subscriber_count == 1200000
        assert result.description == 'News about Python'

    def test_fallback_on_keyerror(self):
        """Falls back to raw API when redditwarp raises KeyError.

        This happens when Reddit removes fields from the API response
        that redditwarp expects (e.g. active_user_count).
        """
        server = self._make_server_with_mock_client()

        # Simulate redditwarp KeyError during model construction
        server.client.p.subreddit.fetch_by_name.side_effect = KeyError('active_user_count')

        # Mock the raw API fallback
        server.client.request.return_value = {
            'data': {
                'display_name': 'python',
                'subscribers': 1200000,
                'public_description': 'News about Python',
            }
        }

        result = server.get_subreddit_info('python')

        assert isinstance(result, SubredditInfo)
        assert result.name == 'python'
        assert result.subscriber_count == 1200000
        assert result.description == 'News about Python'
        server.client.request.assert_called_once_with('GET', '/r/python/about')

    def test_fallback_with_missing_fields(self):
        """Fallback handles missing fields gracefully with defaults."""
        server = self._make_server_with_mock_client()
        server.client.p.subreddit.fetch_by_name.side_effect = KeyError('active_user_count')

        # Minimal API response — missing optional fields
        server.client.request.return_value = {
            'data': {}
        }

        result = server.get_subreddit_info('test_sub')

        assert result.name == 'test_sub'  # falls back to argument
        assert result.subscriber_count == -1  # default
        assert result.description is None

    def test_non_keyerror_exceptions_propagate(self):
        """Non-KeyError exceptions are not caught by the fallback."""
        server = self._make_server_with_mock_client()
        server.client.p.subreddit.fetch_by_name.side_effect = ConnectionError('network down')

        with pytest.raises(ConnectionError):
            server.get_subreddit_info('python')
