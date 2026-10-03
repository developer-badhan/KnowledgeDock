"""Use cases. Each one owns a business rule and orchestrates the layers below.

Routes call these. They never touch a repository, a hasher, or a token directly
(`SKILL.md` §4).
"""
