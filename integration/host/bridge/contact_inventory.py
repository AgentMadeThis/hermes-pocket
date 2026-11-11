"""Permanent peers versus the firmware's USB-only synthetic Meshcomod contact."""
from .core import full_key

MESHCOMOD_KEY = '4d455348434d0000000000000000000000000000000000000000000000000000'
_MESHCOMOD_FIELDS = {'adv_name': 'Meshcomod', 'type': 1, 'flags': 0,
                     'out_path_len': -1, 'out_path': ''}


def permanent_contacts(contacts, owner):
    """Validate all entries; exempt only the exact documented virtual record.

    MyMesh.cpp getMeshcomodContact zero-fills the key after MESHCM and appends
    this record to get_contacts without storing it. Its prefix is reserved by
    isMeshcomodRecipient, so it must never be enrolled as an owner prefix.
    """
    full_key(owner)
    if owner.startswith(MESHCOMOD_KEY[:12]):
        raise ValueError('reserved Meshcomod owner prefix collision')
    if not isinstance(contacts, dict):
        raise ValueError('invalid contact table')
    permanent = []
    for contact in contacts.values():
        if not isinstance(contact, dict):
            raise ValueError('invalid contact')
        key = full_key(contact.get('public_key'))
        if key == MESHCOMOD_KEY:
            if any(type(contact.get(k)) is not type(v) or contact.get(k) != v
                   for k, v in _MESHCOMOD_FIELDS.items()):
                raise ValueError('malformed reserved Meshcomod contact')
        else:
            permanent.append(contact)
    return permanent


def unique_contact(contacts, key):
    permanent = permanent_contacts(contacts, key)
    # Resolve against ALL entries, not a filtered first-prefix SDK lookup.
    matches = [c for c in contacts.values() if c['public_key'].startswith(key[:12])]
    if len(matches) != 1 or matches[0]['public_key'] != key:
        raise ValueError('missing full key or ambiguous six-byte prefix')
    if len(permanent) != 1:
        raise ValueError('exactly one permanent counterpart required')
    return matches[0]
