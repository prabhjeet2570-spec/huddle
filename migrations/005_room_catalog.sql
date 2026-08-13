-- Preserve SQL validation while extending the catalog beyond three rooms.
CREATE TABLE room_catalog (
    id text PRIMARY KEY,
    name text NOT NULL,
    capacity integer NOT NULL CHECK (capacity > 0)
);
INSERT INTO room_catalog (id,name,capacity) VALUES
('cedar','Cedar',4),
('maple','Maple',8),
('birch','Birch',12),
('willow','Willow',2),
('aspen','Aspen',2),
('fern','Fern',3),
('elm','Elm',4),
('sage','Sage',4),
('ivy','Ivy',4),
('oak','Oak',6),
('pine','Pine',6),
('hazel','Hazel',6),
('alder','Alder',8),
('laurel','Laurel',8),
('olive','Olive',10),
('juniper','Juniper',10),
('magnolia','Magnolia',12),
('sequoia','Sequoia',16),
('cypress','Cypress',16),
('grove','Grove',20),
('atrium','Atrium',24),
('forum','Forum',30),
('studio','Studio',6),
('loft','Loft',10);
ALTER TABLE bookings DROP CONSTRAINT bookings_room_id_check;
ALTER TABLE bookings DROP CONSTRAINT bookings_check1;
ALTER TABLE bookings ADD CONSTRAINT bookings_room_catalog_fk FOREIGN KEY(room_id) REFERENCES room_catalog(id);
ALTER TABLE bookings ADD CONSTRAINT bookings_attendees_positive CHECK (attendees > 0);
CREATE FUNCTION check_booking_capacity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.attendees > (SELECT capacity FROM room_catalog WHERE id=NEW.room_id) THEN
        RAISE EXCEPTION 'Group exceeds room capacity' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER booking_capacity BEFORE INSERT OR UPDATE OF room_id,attendees ON bookings
FOR EACH ROW EXECUTE FUNCTION check_booking_capacity();
