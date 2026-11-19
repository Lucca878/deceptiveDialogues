"""Database extensions and schema for the TEST study archive."""

from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

# Extensions are configured by server.py before requests or migrations use them.
db = SQLAlchemy()
migrate = Migrate()


class Participant(db.Model):
    """One pseudonymous participant session; never store the signed cookie or room token."""

    __tablename__ = "participants"

    # This server-generated opaque ID is separate from browser IDs and authentication tokens.
    study_session_id = db.Column(db.String(32), primary_key=True)
    dataset = db.Column(db.String(16), nullable=False, default="test", index=True)
    study_started_at = db.Column(db.String(40), nullable=False)
    consent_accepted = db.Column(db.Boolean, nullable=False, default=False)
    consent_accepted_at = db.Column(db.String(40))
    entry_verified = db.Column(db.Boolean, nullable=False, default=False)
    travel_intro_seen = db.Column(db.Boolean, nullable=False, default=False)
    travel_screened_out = db.Column(db.Boolean, nullable=False, default=False)
    screening_complete_version = db.Column(db.String(64))
    instructions_complete = db.Column(db.Boolean, nullable=False, default=False)
    prolific_redirect_at = db.Column(db.String(40))
    duration_seconds = db.Column(db.Float)

    screening_submissions = db.relationship(
        "ScreeningSubmission", back_populates="participant", cascade="all, delete-orphan",
    )
    page_visits = db.relationship(
        "PageVisit", back_populates="participant", cascade="all, delete-orphan",
        order_by="PageVisit.sequence",
    )
    feedback = db.relationship(
        "Feedback", back_populates="participant", uselist=False, cascade="all, delete-orphan",
    )
    interviewer_rooms = db.relationship(
        "Room", foreign_keys="Room.interviewer_study_session_id", back_populates="interviewer",
    )
    interviewee_rooms = db.relationship(
        "Room", foreign_keys="Room.interviewee_study_session_id", back_populates="interviewee",
    )


class Room(db.Model):
    """One paired dialogue room, its shared condition, and its participants' study data."""

    __tablename__ = "rooms"
    __table_args__ = (
        db.CheckConstraint("condition IN ('feedback', 'control')"),
        db.CheckConstraint("interviewer_study_session_id != interviewee_study_session_id"),
    )

    # Opaque IDs avoid confusing reusable room codes with persistent record identity.
    id = db.Column(db.String(32), primary_key=True)
    dataset = db.Column(db.String(16), nullable=False, default="test", index=True)
    room_code = db.Column(db.String(16), nullable=False, index=True)
    condition = db.Column(db.String(16), nullable=False)
    interviewer_study_session_id = db.Column(
        db.String(32), db.ForeignKey("participants.study_session_id"), nullable=False,
    )
    interviewee_study_session_id = db.Column(
        db.String(32), db.ForeignKey("participants.study_session_id"), nullable=False,
    )
    interviewer_screening_submission_id = db.Column(
        db.String(64), db.ForeignKey("screening_submissions.submission_id"), nullable=False,
    )
    interviewee_screening_submission_id = db.Column(
        db.String(64), db.ForeignKey("screening_submissions.submission_id"), nullable=False,
    )
    created_at = db.Column(db.String(40), nullable=False)
    paired_at = db.Column(db.String(40), nullable=False)
    chat_started_at = db.Column(db.String(40))
    completed_at = db.Column(db.String(40))

    interviewer = db.relationship(
        "Participant", foreign_keys=[interviewer_study_session_id], back_populates="interviewer_rooms",
    )
    interviewee = db.relationship(
        "Participant", foreign_keys=[interviewee_study_session_id], back_populates="interviewee_rooms",
    )
    interviewer_screening = db.relationship(
        "ScreeningSubmission", foreign_keys=[interviewer_screening_submission_id],
    )
    interviewee_screening = db.relationship(
        "ScreeningSubmission", foreign_keys=[interviewee_screening_submission_id],
    )
    messages = db.relationship(
        "Message", back_populates="room", cascade="all, delete-orphan", order_by="Message.sequence",
    )
    dialogues = db.relationship(
        "DialogueRecord", back_populates="room", cascade="all, delete-orphan",
        order_by="DialogueRecord.dialogue_number",
    )
    feedback_submissions = db.relationship(
        "Feedback", back_populates="room", cascade="all, delete-orphan",
    )
    page_visits = db.relationship("PageVisit", back_populates="room")


class ScreeningSubmission(db.Model):
    """A participant's eligible screening attempt and server-selected claim order."""

    __tablename__ = "screening_submissions"

    submission_id = db.Column(db.String(64), primary_key=True)
    participant_id = db.Column(
        db.String(32), db.ForeignKey("participants.study_session_id"), nullable=False, index=True,
    )
    version = db.Column(db.String(64), nullable=False)
    submitted_at = db.Column(db.String(40), nullable=False)
    claim_order = db.Column(db.JSON, nullable=False)

    participant = db.relationship("Participant", back_populates="screening_submissions")
    answers = db.relationship(
        "ScreeningAnswer", back_populates="submission", cascade="all, delete-orphan",
        order_by="ScreeningAnswer.city_id",
    )


class ScreeningAnswer(db.Model):
    """One canonical city response from a screening submission."""

    __tablename__ = "screening_answers"
    __table_args__ = (
        db.UniqueConstraint("submission_id", "city_id"),
        db.CheckConstraint("visited IN ('yes', 'no')"),
        db.CheckConstraint("familiarity BETWEEN 1 AND 7"),
    )

    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(
        db.String(64), db.ForeignKey("screening_submissions.submission_id"), nullable=False, index=True,
    )
    city_id = db.Column(db.String(64), nullable=False)
    visited = db.Column(db.String(3), nullable=False)
    familiarity = db.Column(db.Integer, nullable=False)

    submission = db.relationship("ScreeningSubmission", back_populates="answers")


class Message(db.Model):
    """One chat turn, including private interviewer question-type metadata."""

    __tablename__ = "messages"
    __table_args__ = (
        db.UniqueConstraint("room_id", "sequence"),
        db.CheckConstraint("role IN ('interviewer', 'interviewee')"),
        db.CheckConstraint(
            "(role = 'interviewer' AND question_type IN "
            "('open-ended', 'closed-ended', 'follow-up', 'clarification', 'probing')) "
            "OR (role = 'interviewee' AND question_type IS NULL)"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.String(32), db.ForeignKey("rooms.id"), nullable=False, index=True)
    sequence = db.Column(db.Integer, nullable=False)
    participant_id = db.Column(
        db.String(32), db.ForeignKey("participants.study_session_id"), nullable=False, index=True,
    )
    role = db.Column(db.String(16), nullable=False)
    text = db.Column(db.Text, nullable=False)
    question_type = db.Column(db.String(32))
    is_claim = db.Column(db.Boolean, nullable=False, default=False)
    sent_at = db.Column(db.String(40), nullable=False)

    room = db.relationship("Room", back_populates="messages")
    participant = db.relationship("Participant")


class DialogueRecord(db.Model):
    """One claim, its judgement, and separate conversation/judgement durations."""

    __tablename__ = "dialogues"
    __table_args__ = (
        db.UniqueConstraint("room_id", "dialogue_number"),
        db.CheckConstraint("judgement IN ('true', 'false')"),
        db.CheckConstraint("confidence BETWEEN 0 AND 100"),
    )

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.String(32), db.ForeignKey("rooms.id"), nullable=False, index=True)
    dialogue_number = db.Column(db.Integer, nullable=False)
    city_id = db.Column(db.String(64), nullable=False)
    claim_text = db.Column(db.Text, nullable=False)
    truth_status = db.Column(db.Boolean, nullable=False)
    started_at = db.Column(db.String(40), nullable=False)
    conversation_ended_at = db.Column(db.String(40), nullable=False)
    dialogue_duration_seconds = db.Column(db.Float, nullable=False)
    judgement = db.Column(db.String(5), nullable=False)
    confidence = db.Column(db.Float, nullable=False)
    correct = db.Column(db.Boolean, nullable=False)
    judgement_submitted_at = db.Column(db.String(40), nullable=False)
    judgement_ended_at = db.Column(db.String(40), nullable=False)
    judgement_duration_seconds = db.Column(db.Float, nullable=False)

    room = db.relationship("Room", back_populates="dialogues")


class PageVisit(db.Model):
    """One server-timed visit; exit and duration remain null if no next request occurs."""

    __tablename__ = "page_visits"
    __table_args__ = (db.UniqueConstraint("participant_id", "sequence"),)

    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(
        db.String(32), db.ForeignKey("participants.study_session_id"), nullable=False, index=True,
    )
    room_id = db.Column(db.String(32), db.ForeignKey("rooms.id"), index=True)
    sequence = db.Column(db.Integer, nullable=False)
    page_name = db.Column(db.String(64), nullable=False)
    entered_at = db.Column(db.String(40), nullable=False)
    exited_at = db.Column(db.String(40))
    duration_seconds = db.Column(db.Float)

    participant = db.relationship("Participant", back_populates="page_visits")
    room = db.relationship("Room", back_populates="page_visits")


class Feedback(db.Model):
    """One participant's post-dialogue feedback response."""

    __tablename__ = "feedback"
    __table_args__ = (
        db.CheckConstraint("task_skill BETWEEN 1 AND 7"),
        db.CheckConstraint("task_difficulty BETWEEN 1 AND 7"),
        db.CheckConstraint("task_motivation BETWEEN 1 AND 7"),
    )

    participant_id = db.Column(
        db.String(32), db.ForeignKey("participants.study_session_id"), primary_key=True,
    )
    room_id = db.Column(db.String(32), db.ForeignKey("rooms.id"), nullable=False, index=True)
    task_skill = db.Column(db.Integer, nullable=False)
    task_difficulty = db.Column(db.Integer, nullable=False)
    task_motivation = db.Column(db.Integer, nullable=False)
    strategy = db.Column(db.String(256), nullable=False)
    additional_feedback = db.Column(db.Text, nullable=False, default="")
    submitted_at = db.Column(db.String(40), nullable=False)

    participant = db.relationship("Participant", back_populates="feedback")
    room = db.relationship("Room", back_populates="feedback_submissions")
