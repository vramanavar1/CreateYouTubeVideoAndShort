# Goal
-   Access attached Images and/or a video from a given gmail account & Create a create a YouTube short video and publish it to youtube
after Human Approval (HITL);

# Steps
-   Include pipeline to validate the sent images for Security, PII and other factors to consider before publishing
-   From the given Image(s) and video file and Description; generate a Thumbnail with image in it as is. Use Email Subject for this tumbnail
-   Prefix and sufix this Thumbnail image to the video file
-   Use https://www.youtube.com/watch?v=hOmMi8ysMgU&list=RDhOmMi8ysMgU&start_radio=1 audio only as it's background song
-   Finally generate short url
-   Create a Sink based design to publish this short url to multiple targets; e.g. email, whatsapp group etc

NOTE: Limit 10 Emails per day and max 20 MB file(s) include all attachments