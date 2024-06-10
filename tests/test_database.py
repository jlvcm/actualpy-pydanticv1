import decimal
import json
import tempfile
from datetime import date, timedelta

import pytest
from sqlmodel import Session, create_engine

from actual import ActualError
from actual.database import SQLModel
from actual.queries import (
    create_account,
    create_rule,
    create_splits,
    create_transaction,
    create_transfer,
    get_accounts,
    get_or_create_category,
    get_or_create_payee,
    get_ruleset,
    get_transactions,
)
from actual.rules import Action, Condition, ConditionType, Rule


@pytest.fixture
def session():
    with tempfile.NamedTemporaryFile() as f:
        sqlite_url = f"sqlite:///{f.name}"
        engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session


def test_account_relationships(session):
    today = date.today()
    bank = create_account(session, "Bank", 5000)
    create_account(session, "Savings")
    landlord = get_or_create_payee(session, "Landlord")
    rent = get_or_create_category(session, "Rent")
    rent_payment = create_transaction(session, today, "Bank", "Landlord", "Paying rent", "Rent", -1200)
    utilities_payment = create_transaction(session, today, "Bank", "Landlord", "Utilities", "Rent", -50)
    create_transfer(session, today, "Bank", "Savings", 200, "Saving money")
    session.commit()
    assert bank.balance == decimal.Decimal(3550)
    assert landlord.balance == decimal.Decimal(-1250)
    assert rent.balance == decimal.Decimal(-1250)
    assert rent_payment.category == rent
    assert len(bank.transactions) == 4  # includes starting balance and one transfer
    assert len(landlord.transactions) == 2
    assert len(rent.transactions) == 2
    # let's now void the utilities_payment
    utilities_payment.delete()
    session.commit()
    assert bank.balance == decimal.Decimal(3600)
    assert landlord.balance == decimal.Decimal(-1200)
    assert rent.balance == decimal.Decimal(-1200)
    assert len(bank.transactions) == 3
    assert len(landlord.transactions) == 1
    assert len(rent.transactions) == 1
    # delete the payee and category
    rent.delete()
    landlord.delete()
    session.commit()
    assert rent_payment.category is None
    assert rent_payment.payee is None
    # find the deleted transaction again
    deleted_transaction = get_transactions(
        session, today - timedelta(days=1), today + timedelta(days=1), "Util", bank, include_deleted=True
    )
    assert [utilities_payment] == deleted_transaction
    assert get_accounts(session, "Bank") == [bank]


def test_create_splits(session):
    bank = create_account(session, "Bank")
    t = create_transaction(session, date.today(), bank, category="Dining", amount=-10.0)
    t_taxes = create_transaction(session, date.today(), bank, category="Taxes", amount=-2.5)
    parent_transaction = create_splits(session, [t, t_taxes], notes="Dining")
    # find all children
    trs = get_transactions(session)
    assert len(trs) == 2
    assert t in trs
    assert t_taxes in trs
    assert all(tr.parent == parent_transaction for tr in trs)
    # find all parents
    parents = get_transactions(session, is_parent=True)
    assert len(parents) == 1
    assert len(parents[0].splits) == 2


def test_create_splits_error(session):
    bank = create_account(session, "Bank")
    wallet = create_account(session, "Wallet")
    t1 = create_transaction(session, date.today(), bank, category="Dining", amount=-10.0)
    t2 = create_transaction(session, date.today(), wallet, category="Taxes", amount=-2.5)
    t3 = create_transaction(session, date.today() - timedelta(days=1), bank, category="Taxes", amount=-2.5)
    with pytest.raises(ActualError, match="must be the same for all transactions in splits"):
        create_splits(session, [t1, t2])
    with pytest.raises(ActualError, match="must be the same for all transactions in splits"):
        create_splits(session, [t1, t3])


def test_create_transaction_without_account_error(session):
    with pytest.raises(ActualError):
        create_transaction(session, date.today(), "foo", "")
    with pytest.raises(ActualError):
        create_transaction(session, date.today(), None, "")


def test_rule_insertion_method(session):
    # create one example transaction
    create_transaction(session, date(2024, 1, 4), create_account(session, "Bank"), "")
    session.commit()
    # create and run rule
    action = Action(field="cleared", value=1)
    assert action.as_dict() == {"field": "cleared", "op": "set", "type": "boolean", "value": True}
    condition = Condition(field="date", op=ConditionType.IS_APPROX, value=date(2024, 1, 2))
    assert condition.as_dict() == {"field": "date", "op": "isapprox", "type": "date", "value": "2024-01-02"}
    # test full rule
    rule = Rule(conditions=[condition], actions=[action], operation="all", stage="pre")
    created_rule = create_rule(session, rule, run_immediately=True)
    assert [condition.as_dict()] == json.loads(created_rule.conditions)
    assert [action.as_dict()] == json.loads(created_rule.actions)
    assert created_rule.conditions_op == "and"
    assert created_rule.stage == "pre"
    trs = get_transactions(session)
    assert trs[0].cleared == 1
    session.flush()
    rs = get_ruleset(session)
    assert len(rs.rules) == 1
    assert str(rs) == "If all of these conditions match 'date' isapprox '2024-01-02' then set 'cleared' to 'True'"
