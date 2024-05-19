from random import shuffle
from sqlalchemy import func

from flask import (
    flash,
    redirect,
    render_template,
    request,
    current_app as app,
    url_for,
)
from flask_mailman import EmailMessage

from main import db
from models.cfp import WorkshopProposal, Proposal
from models.event_tickets import EventTicket
from models.site_state import SiteState, refresh_states

from ..common.email import from_email

from . import cfp_review, admin_required


@cfp_review.route("/lottery", methods=["GET", "POST"])
@admin_required
def lottery():
    # In theory this can be extended to other types but currently only workshops & youthworkshops care
    ticketed_proposals = (
        WorkshopProposal.query.filter_by(requires_ticket=True)
        .filter(Proposal.state.in_(["accepted", "finalised"]))
        .all()
    )

    if request.method == "POST":
        winning_tickets = run_lottery(
            [t for t in ticketed_proposals if t.type == "workshop"]
        )
        flash(f"Lottery run for workshops. {len(winning_tickets)} tickets won.")

        winning_tickets = run_lottery(
            [t for t in ticketed_proposals if t.type == "youthworkshop"]
        )
        flash(f"Lottery run for youthworkshops. {len(winning_tickets)} tickets won.")
        return redirect(url_for(".lottery"))

    return render_template(
        "cfp_review/lottery.html", ticketed_proposals=ticketed_proposals
    )


def run_lottery(ticketed_proposals):
    """
    Here are the rules for the lottery.
    * Each user can only have one lottery ticket per workshop
    * A user's lottery tickets are ranked by preference
    * Drawings are done by rank
    * Once a user wins a lottery their other tickets are cancelled
    """
    # Copy because we don't want to change the original
    ticketed_proposals = ticketed_proposals.copy()
    lottery_round = 0

    winning_tickets = []

    app.logger.info(f"Found {len(ticketed_proposals)} proposals to run a lottery for")
    # Lock the lottery
    signup = SiteState.query.get("signup_state")
    if not signup:
        raise Exception("'signup_state' not found.")

    # This is the only state for running the lottery
    signup.state = "run-lottery"
    db.session.commit()
    db.session.flush()
    refresh_states()


    max_rank = db.session.query(func.max(EventTicket.rank)).scalar() + 1
    # Cache this locally so we're not hitting the db and not having to flush() etc.
    proposal_capacities = {p.id: p.get_lottery_capacity() for p in ticketed_proposals}
    winning_tickets = []

    for lottery_round in range(max_rank):
        for proposal in ticketed_proposals:
            tickets_remaining = proposal_capacities[proposal.id]

            tickets_for_round = [t for t in proposal.tickets if t.is_in_lottery_round(lottery_round)]
            shuffle(tickets_for_round)

            if tickets_remaining <= 0:
                for ticket in tickets_for_round:
                    ticket.lost_lottery()
                    db.session.commit()

                continue

            for ticket in tickets_for_round:
                if ticket.ticket_count < tickets_remaining:
                    ticket.won_lottery_and_cancel_others()
                    winning_tickets.append(ticket)
                    tickets_remaining -= ticket.ticket_count
                else:
                    ticket.lost_lottery()
                db.session.commit()

            proposal_capacities[proposal.id] = tickets_remaining

    db.session.flush()
    app.logger.info(
        f"Issued {len(winning_tickets)} winning tickets over {lottery_round} rounds"
    )

    signup.state = "pending-tickets"
    db.session.commit()
    db.session.flush()
    refresh_states()

    # Email winning tickets here
    # We should probably also check for users who didn't win anything?

    app.logger.info("sending emails")
    send_from = from_email("CONTENT_EMAIL")

    for ticket in winning_tickets:
        msg = EmailMessage(
            f"You have a ticket for the workshop '{ticket.proposal.title}'",
            from_email=send_from,
            to=[ticket.user.email],
        )

        msg.body = render_template(
            "emails/event_ticket_won.txt",
            user=ticket.user,
            proposal=ticket.proposal,
        )

    return winning_tickets
