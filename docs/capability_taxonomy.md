# Capability Taxonomy

## Foundation


| #   | ID             | Capability                                                                                                                                                                                                                                                       | Test example                                                                |
| --- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| 1   | foundation_llm | Foundation LLM capability — QA, rewriting, translation, summarization, retrieval, outline generation, extraction, filtering, sorting, comparison, dedup, conflict detection, numeric computation & format conversion, and public-web lookups (GitHub repos/issues, securities/finance, weather) | What will the weather be like in Hong Kong tomorrow? |


## Maps & travel


| #   | ID                | Capability                                                                  | Test example                                                                                                            |
| --- | ----------------- | --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| 2   | poi_nearby_search | POI / nearby search                                                         | Find 3 quiet coffee shops near People's Square in Shanghai, with names and addresses.                  |
| 3   | route_planning    | Route planning (incl. distance & time estimates)                            | Plan a route from People's Square in Shanghai to Hongqiao Railway Station, estimate distance and time. |
| 4   | live_navigation   | Navigation to a specific location                                                | Start navigation to the Bund in Shanghai.                                                                  |
| 5   | hail_ride         | Ride hailing                                                                | Help hail an economy ride from People's Square in Shanghai to the Shanghai Science and Technology Museum.                |
| 6   | plan_trip         | Travel itinerary planning (= POI + route + ticketing + hotel + attractions) | Plan a 3-day Chengdu itinerary with attractions, routes, dining, and hotel-area suggestions.                            |


## Ticketing & booking


| #   | ID               | Capability                                | Test example                                                                                      |
| --- | ---------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------- |
| 7   | book_train       | Train tickets — search / book             | Search second-class high-speed trains from Shanghai Hongqiao to Nanjing South tomorrow afternoon. |
| 8   | book_flight      | Flights — search / book                   | Search flights from Shanghai to Beijing next Monday afternoon, sorted by price and time.          |
| 9   | book_hotel       | Hotels — search / book                    | Search hotels near the Bund in Shanghai for tomorrow night under 500 RMB, list 3 options.         |
| 10  | book_movie_event | Movie / event tickets — search / purchase | Search tonight's movie showtimes in Shanghai for Star Wars with two adjacent seats.               |


## E-commerce


| #   | ID                | Capability                                                 | Test example                                                                                             |
| --- | ----------------- | ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| 11  | search_product    | Product search & recommendation                            | Recommend 3 Android phones under 3000 RMB for students, comparing performance, camera, and battery life. |
| 12  | purchase_guidance | Purchase guidance / add-to-cart / pre-checkout preparation | Find a roughly 10 kg bentonite cat litter product and add it to the cart.                                |
| 13  | order_food        | Food ordering                                              | Order three Mixue peach oolong drinks with default ice and sugar.                                        |
| 14  | track_order       | Order / logistics / after-sales status                     | Check the logistics status of my most recent keyboard order.                                             |


## Calendar & reminders


| #   | ID                 | Capability                                              | Test example                                                                                             |
| --- | ------------------ | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| 15  | read_calendar      | Calendar — read (incl. statistics & time-range queries) | Read my calendar from next Monday to Friday and list meeting names and times by date.                    |
| 16  | write_calendar     | Calendar — write (create / modify / delete)             | Create a removable test calendar event tomorrow at 10:00 AM titled 'RelayAgent test' lasting 30 minutes. |
| 17  | set_alarm_reminder | Alarm / reminder setting                                | Set a test reminder for 9:00 PM tonight: drink water.                                                    |


## Communication


| #   | ID              | Capability                                 | Test example                                                                                                                      |
| --- | --------------- | ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| 18  | read_sms        | SMS — read                                 | Read a summary of my latest SMS.                                                                                                  |
| 19  | send_sms        | SMS — send                                 | Send an SMS to this phone number saying 'I will arrive later'.                                                                    |
| 20  | read_email      | Email — read (search / read)               | Search my most recent email from GitHub and summarize its subject and time in one sentence.                                       |
| 21  | write_email     | Email — write (reply / send / attachments) | Draft an email to [test@example.com](mailto:test@example.com) with subject 'Test' and a one-sentence body saying this is a draft. |
| 22  | lookup_contacts | Contacts lookup & natural-name resolution  | Look up the phone number and email for the contact 'Alex Wang'.                                                                   |
| 23  | phone_call      | Phone call                                 | Prepare to call the contact 'Alex Zhang'.                                                                                         |


## Files & office


| #   | ID                 | Capability                                      | Test example                                                                                                 |
| --- | ------------------ | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| 24  | find_file          | File find & filter                              | Find PDF files modified in the last 7 days whose names contain 'contract', and list filenames and locations. |
| 25  | operate_file       | File operations — copy / move / rename / delete | Copy relayagent_test.txt from Downloads to Documents; if the file is not found, only report the result.      |
| 26  | write_text_file    | Text file creation & writing                    | Create a text file named relayagent_test_note.txt with the content 'This is a test file'.                    |
| 27  | generate_slides    | Slides generation                               | Generate a 5-slide presentation outline on 'Opportunities and risks of on-device AI assistants'.             |
| 28  | find_gallery_image | Gallery image search                            | Find photos containing a whiteboard in the gallery and list the matches.                                     |
| 29  | read_memo          | Notes / memos — read                            | Read my latest memo and summarize it in one sentence.                                                        |
| 30  | write_memo         | Notes / memos — write (create / edit)           | Create a memo titled 'RelayAgent test' with the content 'Finished checking capability examples today'.       |
| 31  | read_task          | Tasks — read                                    | List my incomplete tasks due this week.                                                                      |
| 32  | write_task         | Tasks — write                                   | Add a task 'Submit report' due tomorrow.                                                                     |


## Vertical lookup


| #   | ID                      | Capability                           | Test example                                                                                                         |
| --- | ----------------------- | ------------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| 33  | search_vertical_content | App-specific vertical content search | Search within the current app for 'hidden bookstores in Shanghai' and list the 3 most relevant results with sources. |


## System


| #   | ID                      | Capability                 | Test example                                                                             |
| --- | ----------------------- | -------------------------- | ---------------------------------------------------------------------------------------- |
| 34  | operate_system_settings | System settings operations | Turn on system dark mode; if it is already on, leave it unchanged and report the status. |
