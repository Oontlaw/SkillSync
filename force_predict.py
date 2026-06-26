from app import app
from database import GuildInfo
from ml.forecast import predict_next_24h, train

with app.app_context():
    # Train models for all guilds
    guilds = GuildInfo.query.all()
    for guild in guilds:
        print(f"Training model for guild {guild.guild_id}...")
        result = train(guild.guild_id, days=30)
        print(f"Result: {result}")

        # Predict next 24h
        print(f"Predicting for guild {guild.guild_id}...")
        preds = predict_next_24h(guild.guild_id)
        print(f"Predictions: {preds}")
